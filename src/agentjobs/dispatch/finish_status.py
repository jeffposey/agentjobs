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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from agentjobs.dispatch.finish import (
    SPAWN_DIRNAME,
    finishes_root,
    spawn_log_path,
    spawn_marker_path,
)
from agentjobs.dispatch.ledger import (
    LockHolder,
    read_lock_holder,
    run_lock_path,
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
    """
    root = finishes_root(home)
    if not root.is_dir():
        return None
    try:
        candidates = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:  # pragma: no cover - unreadable home
        return None
    candidates = [entry for entry in candidates if entry.name != SPAWN_DIRNAME]

    def written(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:  # pragma: no cover - removed mid-scan
            return 0.0

    candidates.sort(key=written, reverse=True)
    best: Optional[Path] = None
    best_key = ""
    for entry in candidates[:SCAN_LIMIT]:
        meta = read_meta(entry)
        if str(meta.get("task_id") or "") != task_id:
            continue
        if project_id and str(meta.get("project_id") or "") not in ("", project_id):
            continue
        key = str(meta.get("started_at") or "")
        if best is None or key > best_key:
            best, best_key = entry, key
    return best


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
            )
        )
    return steps


def _gate_from_phases(records: List[Dict[str, Any]]) -> Optional[GateProgress]:
    """The gate's own progress, from what ``scripts/check.py`` wrote as it ran."""
    started: Optional[Dict[str, Any]] = None
    finished: Optional[Dict[str, Any]] = None
    stage = ""
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
            stages_total = _as_int(record.get("stages_total")) or len(record.get("stages") or [])
        elif kind == "gate_stage_started":
            stage = str(record.get("stage") or "")
            stages_total = _as_int(record.get("total")) or stages_total
        elif kind == "gate_stage_finished":
            stages_run = _as_int(record.get("index")) or stages_run + 1
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
        )
    return GateProgress(
        stage=stage,
        stages_run=stages_run,
        stages_total=stages_total,
        running=True,
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
    ended = _moment(finished) or datetime.now(timezone.utc)
    return max(0.0, round((ended - began).total_seconds(), 1))


# ----- liveness ---------------------------------------------------------------


def finish_lock_holder(home: Path, task_id: str) -> Optional[LockHolder]:
    return read_lock_holder(run_lock_path(home, task_id))


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


def read_finish_status(home: Path, task_id: str, project_id: str = "") -> Optional[FinishStatus]:
    """What is happening, or last happened, to this task's branch. None if nothing has.

    ``None`` is the ordinary answer for the overwhelming majority of tasks and means
    exactly what it says: no finish has ever run for this one on this machine, within
    the scan limit. A page renders nothing at all for it.
    """
    directory = newest_finish_directory(home, task_id, project_id)
    marker = read_spawn_marker(home, task_id)
    holder = finish_lock_holder(home, task_id)
    working = holder_is_working(home, holder)

    if directory is None:
        if marker is None:
            return None
        return _starting(task_id, project_id, marker, working)

    meta = read_meta(directory)
    started_at = str(meta.get("started_at") or "")
    # A marker newer than the newest directory is the next finish starting: the one on
    # disk is the previous attempt, and reporting it would show a reader the *last*
    # finish's outcome at the moment they asked about this one.
    if marker is not None and str(marker.get("started_at") or "") > started_at:
        return _starting(task_id, project_id, marker, working)

    records = read_phases(directory)
    steps = _steps_from_phases(records)
    gate = _gate_from_phases(records)
    outcome = str(meta.get("outcome") or "")
    finished_at = str(meta.get("finished_at") or "")

    if finished_at or outcome in (FINISHED, ESCALATED, DECLINED):
        state = outcome if outcome in (FINISHED, ESCALATED, DECLINED) else FINISHED
        live = False
        current = ""
    elif working:
        state = RUNNING
        live = True
        current = _next_step(steps)
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
    return FinishStatus(
        task_id=task_id,
        project_id=str(meta.get("project_id") or project_id),
        state=state,
        live=live,
        finish_id=str(meta.get("finish_id") or directory.name),
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=_elapsed(started_at, finished_at),
        branch=str(preflight.get("branch") or ""),
        worktree=str(preflight.get("worktree") or ""),
        current_step=current,
        steps=steps,
        gate=gate,
        reason=str(meta.get("reason") or ""),
        stopped_at=str(meta.get("stopped_at") or ""),
        merge_commit=str(meta.get("merge_commit") or ""),
        directory=directory,
    )


def _starting(task_id: str, project_id: str, marker: Dict[str, Any], working: bool) -> FinishStatus:
    """The window between the click and the finish's first write.

    Reported as live even with no lock holder yet, because for the first second or two
    there is genuinely nothing to hold it: the process is still importing Python. It
    stops being reported this way the moment a directory exists, which is the same
    moment the lock does.
    """
    started_at = str(marker.get("started_at") or "")
    elapsed = _elapsed(started_at, None)
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
    (``--posture-release``) has no spawn log at all, so the gate's own output is the
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
        gate = status.directory / "gate.log"
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
