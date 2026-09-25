"""What a watcher can see of a scripted finish, while it is still happening.

A finish is the one thing this system does on a human's click that takes minutes and
had no surface at all. ``dispatch.finish`` writes plenty down -- ``meta.yaml``, a
``phases.jsonl``, a ``gate.log``, and the spawned process's own stdout under
``finishes/spawn/`` -- but every one of those was written for a person at a shell
afterwards, or for ``scripts/run_report.py``. Nothing assembled them for somebody
sitting in front of the page they pressed Approve on (task-321).

This module is the reading half, and it is deliberately *only* the reading half: it
starts nothing, waits for nothing and holds no lock. Everything it reports comes off
disk, so a hundred browsers watching one finish costs a hundred small file reads and
changes nothing about how the finish runs.

Three decisions are worth stating because a later reader will otherwise re-derive them.

**Liveness is the run lock's answer, not a clock.** A finish holds the task's run lock
for its whole attempt, and ``ledger.stale_lock_reason`` already knows how to tell a
holder that is working from one whose process is gone -- including the case where the
finish is a dispatched run finishing itself, where the lock is the *run's* and the run
record decides. Re-deriving that here with a heartbeat or a timeout would be a second
staleness rule to keep in step with the first, and the two would disagree the first
time somebody killed a gate. The visible consequence is worth the reuse: a finish
whose machine rebooted mid-gate reads as ``interrupted`` rather than as a spinner that
never stops.

**The starting window is covered by a marker, not by guessing.** Between the click and
the finish directory existing there are one to two seconds of Python start-up. A page
that polled during that window would be told "no finish", stop polling, and show
nothing for the following three minutes. ``spawn_finish`` writes the marker
synchronously *before* it spawns anything, so the approve request cannot return before
the evidence exists.

**The step in flight is inferred from the fixed order, and says so.** A finish records
each step as it lands; nothing records a step *beginning*, except the gate, which is
the one long enough for the distinction to matter. So the step after the last completed
one is reported as running, which is true of a finish that is between steps as well as
one that is in the middle of that step.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.finish import (
    DUPLICATE_KEY,
    SPAWN_DIRNAME,
    finishes_root,
    spawn_log_path,
    spawn_marker_path,
    spawn_root,
)
from agentjobs.dispatch.ledger import (
    LockHolder,
    live_lock_holders,
    read_task_lock_holder,
    split_lock_name,
    stale_lock_reason,
)
from agentjobs.dispatch.phases import read_phases

STARTING = "starting"
RUNNING = "running"
FINISHED = "finished"
ESCALATED = "escalated"
DECLINED = "declined"
INTERRUPTED = "interrupted"
"""A finish that never wrote an ending and whose process is not there any more.

Not an outcome ``dispatch.finish`` can write -- by definition nothing was running to
write it. It is what a reader concludes, and it is the state that would otherwise have
been rendered as "still going" forever.
"""

OVERTAKEN = "overtaken"
"""A finish still running against a task that is already closed, having merged nothing.

The other conclusion a reader draws rather than an outcome anything writes, and the
cheap belt to task-514's braces. The finish that closed a task is the one that merged
it, and it goes on working afterwards -- delivery, the worktree, the branch -- so being
closed is not on its own evidence against a running finish. Having merged nothing *and*
recorded no ``close`` step is: such an attempt cannot be the one that closed the task,
so whatever it is still doing, it is not finishing this task.

task-506 is the state this names. A page reading **Completed**, a slot board reading
**Finishing**, and "Queued for the merge runway" as the visible tail, for twenty minutes
after the merge was done -- three surfaces disagreeing, none of them wrong about what it
had read.
"""

SCAN_LIMIT = 60
"""How many finish directories a lookup will read before giving up on older ones.

Ordered by how recently each directory was written to, so the finish a page is asking
about is at or near the front of the list in every case this exists to serve. The cap
is what stops a machine with a thousand historical finishes paying for all of them on
a two-second poll. A finish older than the cap is simply not offered on the task page;
the task's own record says what happened to it either way.
"""

STEP_ORDER = (
    "preflight",
    "runway",
    "rebase",
    "gate",
    "catch_up",
    "merge",
    "rebuild",
    "restart",
    "verify",
    "close",
    "teardown",
    "worktree",
    "branch",
)
"""The sequence ``dispatch.finish._sequence`` runs, in the order it runs it.

Duplicated from that function rather than derived from it, and the duplication is
checked by a test: the names are what a reader sees, and a list that silently lost a
step would show a finish skipping straight from the merge to the close.
"""

STEP_MEANING = {
    "preflight": "Checking the branch, the worktree and the clone",
    "runway": "Waiting for the repository's merge runway",
    "rebase": "Rebasing the branch onto its base",
    "gate": "Running the full gate on the rebased branch",
    "catch_up": "Re-verifying anything that landed on the base during the gate",
    "merge": "Merging --no-ff",
    "rebuild": "Rebuilding the frontend bundle",
    "restart": "Restarting the server",
    "verify": "Checking the merge is live",
    "close": "Closing the task",
    "teardown": "Stopping what runs out of the worktree",
    "worktree": "Removing the worktree",
    "branch": "Deleting the branch",
}
"""One sentence per step, for a reader who has never read ``ENGINEERING.md``.

The step names are the finish's own vocabulary and stay that way -- they are what the
log entries and the spawn log say -- but "runway" means nothing to somebody watching
their task merge, and this is the only place that can tell them.
"""


@dataclass(frozen=True)
class FinishStep:
    """One step of a finish, as a page should render it."""

    name: str
    #: ``done`` | ``skipped`` | ``stopped`` | ``running`` | ``pending``
    state: str
    detail: str = ""
    seconds: float = 0.0
    meaning: str = ""
    landed_at: str = ""
    """When the finish recorded this step landing, from the phase record. What the landing
    estimate measures a step by: landing to landing, so time between steps counts."""


@dataclass(frozen=True)
class GateProgress:
    """How far into the gate this finish is, when the gate is what it is doing.

    The gate is around 85% of a finish's wall clock and used to be one silent block, so
    this is the difference between "something is happening" and "seven stages of ten,
    currently pytest". It comes from records ``scripts/check.py`` writes as it goes; a
    gate run by a build of the gate too old to write them reports its stage list and no
    position, which renders as the gate running with no counter rather than as an error.
    """

    stage: str = ""
    stages_run: int = 0
    stages_total: int = 0
    running: bool = False
    passed: Optional[bool] = None
    seconds: float = 0.0
    failed_stage: str = ""
    stages_done: Tuple[str, ...] = ()
    """The stages this gate has finished, in order. What the landing estimate weights."""
    stage_started_at: str = ""
    """When the stage in flight began, so the estimate can count time inside it."""


@dataclass(frozen=True)
class FinishStatus:
    """Everything a page needs to say what is happening to a task right now."""

    task_id: str
    project_id: str
    state: str
    live: bool
    finish_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: Optional[float] = None
    branch: str = ""
    worktree: str = ""
    current_step: str = ""
    steps: List[FinishStep] = field(default_factory=list)
    gate: Optional[GateProgress] = None
    reason: str = ""
    stopped_at: str = ""
    merge_commit: str = ""
    directory: Optional[Path] = None
    earlier_merge_commit: str = ""
    """A merge an *earlier* attempt made of this branch, when this attempt made none.

    A second, separately labelled fact (task-322). ``state`` and ``merge_commit`` still
    describe the newest attempt alone -- an ``escalated`` retry that merged nothing is
    exactly that -- and this is what stops the page saying "nothing was merged" about a
    branch that is already in the base.
    """
    earlier_merge_finish_id: str = ""
    gate_retry: Optional[Dict[str, Any]] = None
    """The one gate retry this attempt made, if it made one: the red stage and the verdict."""
    next_action: str = ""
    """What a person should do now, in one sentence. Empty while nothing is theirs to do."""

    @property
    def merged(self) -> bool:
        return bool(self.merge_commit)


# ----- the marker a spawn leaves behind ---------------------------------------


def read_spawn_marker(home: Path, task_id: str) -> Optional[Dict[str, Any]]:
    path = spawn_marker_path(home, task_id)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


# ----- finding the finish a task is in ----------------------------------------


def read_meta(directory: Path) -> Dict[str, Any]:
    """A finish directory's metadata, or an empty mapping when there is none."""
    path = directory / "meta.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def newest_finish_directory(home: Path, task_id: str, project_id: str = "") -> Optional[Path]:
    """The most recent finish directory for this task, within the scan limit.

    Directories are visited most-recently-written first so that the one being asked
    about is found immediately, and the *newest by ``started_at``* is returned rather
    than the first hit -- filesystem mtime orders activity, and a retry writing into an
    older directory would otherwise win on it.

    An attempt carrying :data:`agentjobs.dispatch.finish.DUPLICATE_KEY` is skipped
    (task-514). It declined at the front door because another finish has this task, so
    it is the newest directory on disk and the wrong answer to "what is happening to
    this task" -- the finish it duplicates is. Kept on disk, off every surface.
    """
    root = finishes_root(home)
    if not root.is_dir():
        return None
    try:
        candidates = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:  # pragma: no cover - unreadable home
        return None
    candidates = [entry for entry in candidates if entry.name != SPAWN_DIRNAME]

    candidates.sort(key=_written, reverse=True)
    best: Optional[Path] = None
    best_key = ""
    for entry in candidates[:SCAN_LIMIT]:
        meta = read_meta(entry)
        if str(meta.get("task_id") or "") != task_id:
            continue
        if project_id and str(meta.get("project_id") or "") not in ("", project_id):
            continue
        if meta.get(DUPLICATE_KEY):
            continue
        key = str(meta.get("started_at") or "")
        if best is None or key > best_key:
            best, best_key = entry, key
    return best


def _written(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - removed mid-scan
        return 0.0


# ----- what the phase records say ---------------------------------------------


def _steps_from_phases(records: List[Dict[str, Any]]) -> List[FinishStep]:
    """Completed steps, in the order the finish recorded them landing."""
    steps: List[FinishStep] = []
    for record in records:
        if record.get("kind") != "finish_step":
            continue
        name = str(record.get("step") or "")
        if not name:
            continue
        if record.get("skipped"):
            state = "skipped"
        elif record.get("ok"):
            state = "done"
        else:
            state = "stopped"
        steps.append(
            FinishStep(
                name=name,
                state=state,
                detail=str(record.get("detail") or ""),
                seconds=_as_float(record.get("seconds")),
                meaning=STEP_MEANING.get(name, ""),
                landed_at=str(record.get("ts") or ""),
            )
        )
    return steps


def _gate_from_phases(records: List[Dict[str, Any]]) -> Optional[GateProgress]:
    """The gate's own progress, from what ``scripts/check.py`` wrote as it ran."""
    started: Optional[Dict[str, Any]] = None
    finished: Optional[Dict[str, Any]] = None
    stage = ""
    stage_started_at = ""
    stages_done: List[str] = []
    stages_run = 0
    stages_total = 0
    for record in records:
        kind = record.get("kind")
        if kind == "gate_started":
            started = record
            # A new gate resets everything a previous one on this finish left behind.
            # Only a retried finish reuses a directory, but when one does, the first
            # gate's stage counter must not be what the second one is rendered from.
            finished, stage, stages_run = None, "", 0
            stage_started_at, stages_done = "", []
            stages_total = _as_int(record.get("stages_total")) or len(record.get("stages") or [])
        elif kind == "gate_stage_started":
            stage = str(record.get("stage") or "")
            stage_started_at = str(record.get("ts") or "")
            stages_total = _as_int(record.get("total")) or stages_total
        elif kind == "gate_stage_finished":
            stages_run = _as_int(record.get("index")) or stages_run + 1
            finished_stage = str(record.get("stage") or "")
            if finished_stage:
                stages_done.append(finished_stage)
            stages_total = _as_int(record.get("total")) or stages_total
        elif kind == "gate_finished":
            finished = record
    if started is None:
        return None
    if finished is not None:
        return GateProgress(
            stage="",
            stages_run=_as_int(finished.get("stages_run")),
            stages_total=_as_int(finished.get("stages_total")) or stages_total,
            running=False,
            passed=bool(finished.get("passed")),
            seconds=_as_float(finished.get("seconds")),
            failed_stage=str(finished.get("failed_stage") or ""),
            stages_done=tuple(stages_done),
        )
    return GateProgress(
        stage=stage,
        stages_run=stages_run,
        stages_total=stages_total,
        running=True,
        stages_done=tuple(stages_done),
        stage_started_at=stage_started_at,
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _moment(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _elapsed(started: Any, finished: Any) -> Optional[float]:
    """Seconds a finish has been going, or ran for. Computed here, never in a browser.

    The same rule the runs list follows and for the same reason: ``started_at`` is this
    machine's clock, and a phone reading the page is not on it.
    """
    began = _moment(started)
    if began is None:
        return None
    ended = _moment(finished) or dispatch_clock.utcnow()
    return max(0.0, round((ended - began).total_seconds(), 1))


# ----- liveness ---------------------------------------------------------------


def finish_lock_holder(home: Path, task_id: str, *, project_id: str) -> Optional[LockHolder]:
    """The holder of this project's task lock, or of a pre-task-264 unscoped one."""
    return read_task_lock_holder(home, task_id, project_id=project_id)


def holder_is_working(home: Path, holder: Optional[LockHolder]) -> bool:
    """Whether the thing holding this task's lock is still holding anything.

    Delegated to ``stale_lock_reason`` in full, so this agrees with the refusal a
    dispatch would get by construction rather than by inspection. It covers both shapes
    a finish comes in: its own detached process, whose pid decides, and a dispatched run
    finishing itself, where the lock names the run and the run record decides.
    """
    if holder is None:
        return False
    return stale_lock_reason(home, holder) is None


# ----- assembling one status --------------------------------------------------


def read_finish_status(
    home: Path,
    task_id: str,
    project_id: str = "",
    *,
    task_open: Optional[bool] = None,
) -> Optional[FinishStatus]:
    """What is happening, or last happened, to this task's branch. None if nothing has.

    ``None`` is the ordinary answer for the overwhelming majority of tasks and means
    exactly what it says: no finish has ever run for this one on this machine, within
    the scan limit. A page renders nothing at all for it.

    ``task_open`` is the caller's answer to "is this task still open", passed in rather
    than read here because every caller already holds the task and this module opens no
    store (task-514). ``None`` means the caller did not say, and nothing changes;
    ``False`` is what turns a running finish that merged nothing into :data:`OVERTAKEN`.
    """
    return _status_of(
        home,
        task_id,
        project_id,
        newest_finish_directory(home, task_id, project_id),
        task_open=task_open,
    )


def _status_of(
    home: Path,
    task_id: str,
    project_id: str,
    directory: Optional[Path],
    *,
    task_open: Optional[bool] = None,
) -> Optional[FinishStatus]:
    """:func:`read_finish_status`, once the task's newest finish directory is known.

    Split out so a caller that has *already* found that directory does not pay for
    finding it again. :func:`live_finishes` is the one such caller: it resolves the
    newest directory for every candidate from a single scan, and then asks this the same
    question the task page asks, about the same attempt, through the same code. That is
    what stops the chip a list draws and the panel a page draws from ever disagreeing --
    there is one liveness rule, not a batched copy of one.
    """
    marker = read_spawn_marker(home, task_id)
    if directory is None and marker is None:
        # Nothing has ever finished this task, so there is no question to answer and no
        # reason to ask the lock. Asked before `holder_is_working` rather than after
        # because that call reaches `process_identity`, which is the single most
        # expensive read in this module -- it inspects a live process -- and a batched
        # caller asks about tasks in this state far more often than about any other.
        return None

    holder = finish_lock_holder(home, task_id, project_id=project_id)
    working = holder_is_working(home, holder)

    if directory is None:
        assert marker is not None  # the pair is excluded above
        return _starting(task_id, project_id, marker, working, task_open=task_open)

    meta = read_meta(directory)
    started_at = str(meta.get("started_at") or "")
    # A marker newer than the newest directory is the next finish starting: the one on
    # disk is the previous attempt, and reporting it would show a reader the *last*
    # finish's outcome at the moment they asked about this one.
    if marker is not None and str(marker.get("started_at") or "") > started_at:
        return _starting(task_id, project_id, marker, working, task_open=task_open)

    records = read_phases(directory)
    steps = _steps_from_phases(records)
    gate = _gate_from_phases(records)
    outcome = str(meta.get("outcome") or "")
    finished_at = str(meta.get("finished_at") or "")

    if finished_at or outcome in (FINISHED, ESCALATED, DECLINED):
        state = outcome if outcome in (FINISHED, ESCALATED, DECLINED) else FINISHED
        live = False
        current = ""
    elif working and not _overtaken(task_open, meta, records, steps):
        state = RUNNING
        live = True
        current = _next_step(steps)
    elif working:
        state = OVERTAKEN
        live = False
        current = ""
    else:
        state = INTERRUPTED
        live = False
        current = ""

    if current:
        steps = steps + [
            FinishStep(
                name=current,
                state="running",
                detail="",
                seconds=0.0,
                meaning=STEP_MEANING.get(current, ""),
            )
        ]

    preflight = next((record for record in records if record.get("kind") == "finish_preflight"), {})
    resolved_project = str(meta.get("project_id") or project_id)
    branch = str(preflight.get("branch") or "")
    merge_commit = merge_commit_of(meta, records)
    earlier_commit, earlier_finish = ("", "")
    if not merge_commit:
        earlier_commit, earlier_finish = earlier_merge(
            home, task_id, resolved_project, exclude=directory, branch=branch
        )
    retry = next(
        (
            record
            for record in reversed(records)
            if record.get("kind") in ("finish_gate_retry", "finish_gate_red_twice")
        ),
        None,
    )
    status = FinishStatus(
        task_id=task_id,
        project_id=resolved_project,
        state=state,
        live=live,
        finish_id=str(meta.get("finish_id") or directory.name),
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=_elapsed(started_at, finished_at),
        branch=branch,
        worktree=str(preflight.get("worktree") or ""),
        current_step=current,
        steps=steps,
        gate=gate,
        reason=str(meta.get("reason") or ""),
        stopped_at=str(meta.get("stopped_at") or ""),
        merge_commit=merge_commit,
        directory=directory,
        earlier_merge_commit=earlier_commit,
        earlier_merge_finish_id=earlier_finish,
        gate_retry=(
            {
                "failed_stage": str(retry.get("failed_stage") or ""),
                "classification": str(retry.get("classification") or ""),
                "explanation": str(retry.get("explanation") or ""),
            }
            if retry is not None
            else None
        ),
    )
    return replace(status, next_action=next_action(status, meta))


def live_finishes(home: Path, project_id: str = "") -> Dict[str, FinishStatus]:
    """Every task a finish is *live* for right now, for a caller holding many tasks.

    The batched form of :func:`read_finish_status`, written for the task list: a list of
    500 rows asking the per-task form would resolve the newest finish directory 500
    times over for a fact that is ``None`` on all but one or two of them (task-509).

    **Nothing here judges liveness.** The two halves below only decide *whom to ask
    about*; :func:`_status_of` gives the answer, about the same attempt and through the
    same code the task page's panel reads. So the chip a list draws and the panel a page
    draws cannot disagree.

    **What it costs, measured on this machine on 2026-09-20** against the live home --
    258 finish directories, 43 spawn markers, three agent runs holding task locks:

    - **Nothing in the project holds a lock: 1.7 ms**, and not one finish directory
      read. The candidate scan comes first precisely so that this case exists: with no
      candidate there is nothing to resolve a directory *for*, so the expensive half
      never runs at all.
    - **Candidates present: 86 ms**, of which 41 ms is the one shared scan and the rest
      is confirming them. A candidate with no finish behind it costs 0.03 ms; one with a
      finish directory to read costs about 45 ms, which is what a task page pays for the
      same task and for the same reasons.

    **Neither figure moves with the number of tasks**, which is the property that
    matters and the one ``tests/test_performance_budgets.py`` asserts: a list of 500 rows
    pays what a list of one pays, because the work is bounded by the machine's live locks
    rather than by the corpus. The per-task alternative was measured beside it at 42 ms
    a row -- **21 seconds** for a 500-row list -- which is the whole reason this exists.
    """
    candidates = _finishing_candidates(home, project_id)
    if not candidates:
        return {}
    unresolved = {task_id for task_id, named in candidates.items() if named is None}
    scanned = _newest_directories(home, project_id, unresolved) if unresolved else {}
    live: Dict[str, FinishStatus] = {}
    for task_id in sorted(candidates):
        directory = candidates[task_id] or scanned.get(task_id)
        status = _status_of(home, task_id, project_id, directory)
        if status is not None and status.live:
            live[task_id] = status
    return live


def machine_live_finishes(
    home: Path, project_ids: Sequence[str] = ()
) -> Dict[Tuple[str, str], FinishStatus]:
    """Every live finish on this machine, keyed ``(project_id, task_id)`` (task-533).

    :func:`live_finishes` for the machine-wide runs surface, which holds runs from every
    project at once. It is that function called once per project that could have a
    finish, and nothing else: the liveness answer is still :func:`_status_of`'s, so the
    slot board's tile and the task read's chip are the same fact read the same way.

    **Why per project rather than ``live_finishes(home, "")``.** An empty project id
    reads only a pre-task-264 unscoped lock, so every scoped finish would confirm as
    ``interrupted`` and nothing would ever read as finishing.

    **Which projects get asked.** Those named by a held lock, plus ``project_ids`` --
    the caller's live runs, which covers a spawn marker's second-long window before its
    process takes a lock. Both are bounded by what is running on the machine, never by
    how many tasks or rows exist, so this costs what ``live_finishes`` costs times the
    number of projects with something running -- in practice one or two. Measured
    figures are in ``api.routes.runs.list_live_runs``.
    """
    try:
        holders = live_lock_holders(home)
    except OSError:  # pragma: no cover - unreadable home
        holders = []
    projects = {split_lock_name(name)[0] for name, holder in holders if not holder.is_runway}
    projects.update(project_ids)
    projects.discard("")
    found: Dict[Tuple[str, str], FinishStatus] = {}
    for project_id in sorted(projects):
        for task_id, status in live_finishes(home, project_id).items():
            found[(project_id, task_id)] = status
    return found


def _finishing_candidates(home: Path, project_id: str) -> Dict[str, Optional[Path]]:
    """Tasks that could have a finish in flight, mapped to its directory where that is known.

    Deliberately generous and deliberately cheap. Every task named here is confirmed or
    dropped by :func:`_status_of`, so a false candidate costs a lookup and never a wrong
    answer; a task *missing* from here would be a finish nobody is told about, which is
    the failure that matters.

    **The locks are the first half, because liveness is the lock's answer** -- the rule
    this module opens with. A finish holds its task's run lock for the whole attempt, and
    ``live_lock_holders`` has already applied ``stale_lock_reason``, so a held lock is a
    process that is really working.

    **A ``kind=finish`` lock names its attempt, so it needs no scan at all.** The lock
    adopts the finish id a moment after creating the directory (``RunLock.adopt_finish``,
    task-298), which makes it the cheapest possible answer to "which directory": a
    ``Path``, resolved from the lock, for the shape every approved finish comes in.

    **Ordinary dispatch locks are candidates too, and they are the reason a scan still
    exists.** A run finishing *itself* under ``--merge-mode-release`` keeps its own
    ``kind=run`` lock and never adopts a finish one (``dispatch.finish.run_finish``), so
    filtering on ``holder.is_finish`` would miss exactly the case an autonomous project
    merges through. Such a candidate maps to ``None`` and is resolved by the shared scan,
    which is also what confirms -- cheaply, from its own newest attempt being terminal or
    absent -- that an agent merely *working* a task is not finishing it.

    **The markers are the second half**, for the one window the locks cannot see: between
    ``spawn_finish`` writing its marker and the spawned process taking the lock a second
    or two later. Bounded by ``STARTING_GRACE_SECONDS``, the same constant ``_starting``
    judges that window against, so this asks the markers nothing the confirmation would
    not also ask rather than introducing a second rule.
    """
    candidates: Dict[str, Optional[Path]] = {}
    try:
        holders = live_lock_holders(home)
    except OSError:  # pragma: no cover - unreadable home
        holders = []
    for name, holder in holders:
        if holder.is_runway:
            continue
        held_project, task_id = split_lock_name(name)
        if not task_id:
            continue
        if project_id and held_project not in ("", project_id):
            continue
        named: Optional[Path] = None
        if holder.finish_id:
            candidate = finishes_root(home) / holder.finish_id
            # A lock may name a directory for the instant between the two writes that
            # create them. Falling back to the scan there is correct and costs nothing
            # that is not already being paid.
            named = candidate if candidate.is_dir() else None
        candidates[task_id] = named
    horizon = time.time() - STARTING_GRACE_SECONDS
    spawn = spawn_root(home)
    if spawn.is_dir():
        try:
            markers = list(spawn.glob("*.json"))
        except OSError:  # pragma: no cover - unreadable home
            markers = []
        for marker in markers:
            if _written(marker) >= horizon:
                candidates.setdefault(marker.stem, None)
    return candidates


def _newest_directories(
    home: Path, project_id: str, task_ids: Set[str]
) -> Dict[str, Optional[Path]]:
    """Each task's newest finish directory, from **one** scan of the finishes root.

    :func:`newest_finish_directory` for a set of tasks rather than for one, and it keeps
    that function's two rules exactly: directories are visited most-recently-written
    first and capped at ``SCAN_LIMIT``, so this and a task page agree about which
    attempts are old enough to have fallen out of view; and the newest attempt is the one
    with the greatest ``started_at`` rather than the first hit, because a retry writing
    into an older directory would otherwise win on mtime and hide the attempt that
    matters.

    A task with no directory inside the cap maps to ``None`` rather than being absent, so
    the caller still asks about it: the spawn marker alone answers for a finish whose
    directory does not exist yet. An attempt that declined as a duplicate is skipped, for
    the reason :func:`newest_finish_directory` gives.
    """
    newest: Dict[str, Optional[Path]] = {task_id: None for task_id in task_ids}
    started: Dict[str, str] = {}
    root = finishes_root(home)
    if not root.is_dir():
        return newest
    try:
        entries = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:  # pragma: no cover - unreadable home
        return newest
    entries = [entry for entry in entries if entry.name != SPAWN_DIRNAME]
    entries.sort(key=_written, reverse=True)
    for entry in entries[:SCAN_LIMIT]:
        meta = read_meta(entry)
        task_id = str(meta.get("task_id") or "")
        if task_id not in newest:
            continue
        if project_id and str(meta.get("project_id") or "") not in ("", project_id):
            continue
        if meta.get(DUPLICATE_KEY):
            # The same rule the per-task scan applies, and it has to be here too or the
            # list and the page disagree about which attempt is the task's (task-514).
            continue
        key = str(meta.get("started_at") or "")
        if newest[task_id] is None or key > started.get(task_id, ""):
            newest[task_id], started[task_id] = entry, key
    return newest


def did_the_closing(merge_commit: str, steps: Sequence[FinishStep]) -> bool:
    """Whether this attempt is plausibly the finish that closed the task (task-514).

    Two ways to be, and both are the ordinary case rather than padding: it merged
    something, or it recorded the ``close`` step -- which is step ten of twelve, so such
    a finish goes on working for a second or two afterwards, removing the worktree and
    deleting the branch. Either way, a task that is closed with this running is the
    expected state rather than the disagreement task-506 was.

    The one rule, read by :func:`_overtaken` for the task page's panel and by
    ``api.live_finish`` for the chip a list draws, so those two cannot disagree.
    """
    if merge_commit:
        return True
    return any(step.name == "close" and step.state in ("done", "skipped") for step in steps)


def _overtaken(
    task_open: Optional[bool],
    meta: Dict[str, Any],
    records: List[Dict[str, Any]],
    steps: List[FinishStep],
) -> bool:
    """Whether a finish still holding this task cannot be the one that finished it.

    See :data:`OVERTAKEN`.
    """
    if task_open is not False:
        return False
    return not did_the_closing(merge_commit_of(meta, records), steps)


def merge_commit_of(meta: Dict[str, Any], records: List[Dict[str, Any]]) -> str:
    """The merge this attempt made: its meta, or the phase record written the moment it did.

    The phase record is what makes a merge visible on an attempt that was *killed* after
    merging (task-322): such an attempt never writes the ending its meta would carry, and
    task-321's own ``fin_d11a8f5e`` was exactly that shape.
    """
    committed = str(meta.get("merge_commit") or "")
    if committed:
        return committed
    for record in reversed(records):
        if record.get("kind") == "finish_merged" and record.get("merge_commit"):
            return str(record["merge_commit"])
    return ""


def earlier_merge(
    home: Path, task_id: str, project_id: str, *, exclude: Path, branch: str
) -> Tuple[str, str]:
    """``(commit, finish_id)`` of a merge an earlier attempt made of this branch, or blanks.

    Two sources, in order. The task's finish receipts, which survive every attempt and say
    which branch each merge was of. Then the other finish directories within the scan
    limit, which is what an attempt from before receipts existed left behind. A merge of a
    *different* branch than the newest attempt's is not reported: a reopened task on a new
    branch has not been merged just because its previous branch was.
    """
    from agentjobs.dispatch.finish_receipts import FinishReceipts

    evidence = FinishReceipts(home, project_id, task_id).applied_merge() if project_id else None
    if evidence is not None and (not branch or not evidence.branch or evidence.branch == branch):
        return evidence.commit, evidence.finish_id
    root = finishes_root(home)
    try:
        candidates = [
            entry
            for entry in root.iterdir()
            if entry.is_dir() and entry.name != SPAWN_DIRNAME and entry != exclude
        ]
    except OSError:
        return "", ""
    candidates.sort(key=_written, reverse=True)
    found: List[Tuple[str, str, str]] = []
    for entry in candidates[:SCAN_LIMIT]:
        meta = read_meta(entry)
        if str(meta.get("task_id") or "") != task_id:
            continue
        if project_id and str(meta.get("project_id") or "") not in ("", project_id):
            continue
        records = read_phases(entry)
        commit = merge_commit_of(meta, records)
        if not commit:
            continue
        merged_branch = next(
            (
                str(record.get("branch") or "")
                for record in records
                if record.get("kind") in ("finish_preflight", "finish_merged")
            ),
            "",
        )
        if branch and merged_branch and merged_branch != branch:
            continue
        found.append((str(meta.get("started_at") or ""), commit, entry.name))
    if not found:
        return "", ""
    _, commit, finish_id = max(found)
    return commit, finish_id


WITHDRAWN_REASONS = frozenset({"stopped", "approval_withdrawn", "automerge_withdrawn"})


def next_action(status: FinishStatus, meta: Dict[str, Any]) -> str:
    """The one thing a person should do about this finish now, or nothing.

    Written on the server so the page and any other reader say the same thing, and
    derived only from what the attempt recorded. A command is named where one is the
    action, because "re-run the finish" is not something a person can do without it.
    """
    command = f"agentjobs finish {status.task_id} --project {status.project_id}"
    merged = status.merge_commit or status.earlier_merge_commit
    if status.live or status.state in (FINISHED, DECLINED, STARTING, RUNNING):
        return ""
    dispatched = str(meta.get("dispatched_run_id") or "")
    if status.state == ESCALATED and dispatched:
        return (
            f"A session ({dispatched}) was started to take it from here. Nothing is yours "
            "to do unless it hands the task back."
        )
    if status.state == OVERTAKEN:
        return (
            "Nothing here is yours to do: this attempt merged nothing and the task is "
            "already closed, so it is not the finish that finished it. Its own record "
            "says what it got as far as."
        )
    if status.reason == "stopped_after_merge" or (
        merged and status.state in (ESCALATED, INTERRUPTED)
    ):
        return (
            f"The merge is done ({merged[:8]}); delivery is not. Re-run the finish to resume "
            f"it from where it stopped -- it will not gate or merge again: `{command}`."
        )
    if status.reason in WITHDRAWN_REASONS:
        return (
            "Nothing was merged, because what authorised it was withdrawn. Nothing further "
            "happens until the task is approved again."
        )
    if status.state == INTERRUPTED:
        return f"Nothing was merged. Re-run the finish to start it again: `{command}`."
    return (
        f"Nothing was merged. Fix what stopped it at `{status.stopped_at or 'a step'}` -- the "
        f"task record says what that was -- then re-run the finish: `{command}`."
    )


def _starting(
    task_id: str,
    project_id: str,
    marker: Dict[str, Any],
    working: bool,
    *,
    task_open: Optional[bool] = None,
) -> FinishStatus:
    """The window between the click and the finish's first write.

    Reported as live even with no lock holder yet, because for the first second or two
    there is genuinely nothing to hold it: the process is still importing Python. It
    stops being reported this way the moment a directory exists, which is the same
    moment the lock does.

    A marker for a **closed** task is the one shape of this that is never worth waiting
    on (task-514): a finish that has not started has merged nothing, so it cannot be the
    one that closed the task, and the grace period would otherwise have a page polling a
    completed task for a minute.
    """
    started_at = str(marker.get("started_at") or "")
    elapsed = _elapsed(started_at, None)
    if task_open is False:
        return FinishStatus(
            task_id=task_id,
            project_id=str(marker.get("project_id") or project_id),
            state=OVERTAKEN,
            live=False,
            started_at=started_at,
            elapsed_seconds=elapsed,
            steps=[],
        )
    stale = elapsed is not None and elapsed > STARTING_GRACE_SECONDS and not working
    return FinishStatus(
        task_id=task_id,
        project_id=str(marker.get("project_id") or project_id),
        state=INTERRUPTED if stale else STARTING,
        live=not stale,
        started_at=started_at,
        elapsed_seconds=elapsed,
        current_step="" if stale else STEP_ORDER[0],
        steps=[],
        reason="never_started" if stale else "",
    )


STARTING_GRACE_SECONDS = 60.0
"""How long a spawned finish may take to write its first record before it is doubted.

The only clock in this module, and it is confined to the one state where there is
nothing on disk to reason from. Generous by an order of magnitude: the gap measured on
this machine is one to two seconds, and the cost of being wrong in the patient
direction is a reader waiting a minute rather than a reader told a live finish is dead.
"""


def _next_step(done: List[FinishStep]) -> str:
    """The step a live finish is on, inferred from the last one it finished.

    A stopped step means the finish is escalating rather than working, so nothing is
    reported as running after one.
    """
    if done and done[-1].state == "stopped":
        return ""
    if not done:
        return STEP_ORDER[0]
    last = done[-1].name
    try:
        index = STEP_ORDER.index(last)
    except ValueError:
        return ""
    return STEP_ORDER[index + 1] if index + 1 < len(STEP_ORDER) else ""


# ----- the text a person reads ------------------------------------------------


def finish_output(home: Path, status: FinishStatus) -> Tuple[str, str, Optional[float]]:
    """This finish's output as a human should read it: (source, text, last changed).

    Two files, and which one is right depends on how the finish was started. A finish
    spawned by an approval writes its whole step table to ``finishes/spawn/<task>.log``
    when it ends -- that is the script output the reader asked for, and it does not
    exist until the process is over. A finish run inside a dispatched session
    (``--merge-mode-release``) has no spawn log at all, so the gate's own output is the
    only text there is.

    An empty answer while a finish is running is the normal case and not a failure: the
    step table is what is live, and it comes from the phase records rather than from
    text.
    """
    spawn = spawn_log_path(home, status.task_id)
    text = _read(spawn)
    if text.strip():
        return "finish-log", text, _mtime(spawn)
    if status.directory is not None:
        # The retry's log before the first attempt's: it is the newer answer, and the one
        # a person reading a second red needs (task-322).
        for name in ("gate-retry-1.log", "gate.log"):
            gate = status.directory / name
            text = _read(gate)
            if text.strip():
                return "gate-log", text, _mtime(gate)
    return "none", "", None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - removed between read and stat
        return None
