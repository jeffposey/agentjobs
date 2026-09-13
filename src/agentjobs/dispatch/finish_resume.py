"""Resuming a scripted finish whose process died, without waiting for somebody to notice.

Task-322 made a re-run of ``agentjobs finish`` safe after a kill: it proves an earlier
merge from its receipts, skips the gate, and resumes delivery. Nothing ran it. A finish
killed by a reboot or a killed process writes no ending, consumes no approval, and leaves
its task at ``agent``/``work`` with nobody on it -- the state task-340 forbids -- until a
person reads the panel and types the command it names (task-443).

This module is the thing that types it. **It decides once per attempt, and the decision
is written into that attempt's directory before anything is spawned**, so the rule "resume
once, then park for a human" survives the server restarting between the two.

Three rules worth stating, because each was a choice:

- **Only an attempt that recorded what authorised it is resumed on that authority.**
  ``FinishDirectory.create`` writes ``authority`` and, for a posture finish, the run whose
  grant it merged on. An attempt from before that field existed is left alone rather than
  inferred: resuming a finish somebody killed weeks ago, on deploy, is not a recovery
  anybody asked for.
- **The authority is re-read now, not believed from the attempt.** An approval that no
  longer stands, a Stop since the attempt began, or a run that was cancelled means no
  resume and no park -- whoever withdrew it has already written what the task needs.
- **A resume that fails parks, whatever the failure.** Interrupted again, declined, or
  never started: each is one more attempt nobody is watching, and the task goes to
  ``human``/``decision`` saying whether ``main`` moved.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentjobs.actors import FINISHER
from agentjobs.dispatch.finish import (
    APPROVAL,
    DECLINED,
    POSTURE,
    SPAWN_DIRNAME,
    AuthorityGuard,
    FinishDirectory,
    finish_is_offered,
    finishes_root,
    spawn_finish,
)
from agentjobs.dispatch.finish_receipts import RECEIPTS_DIRNAME
from agentjobs.dispatch.finish_status import (
    INTERRUPTED,
    SCAN_LIMIT,
    FinishStatus,
    read_finish_status,
    read_meta,
)
from agentjobs.dispatch.ledger import LedgerError, find_run, process_alive
from agentjobs.models_v2 import Ball, BallReason
from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.store_factory import TaskManagerLike, dispatch_manager_for

RESUME_CLAIM = "auto-resume.json"
"""Created exclusively in an attempt's directory by whoever spends its one resume."""

PARK_CLAIM = "auto-park.json"
"""Created exclusively in an attempt's directory by whoever parks its task."""

RESUMED = "resumed"
PARKED = "parked"
SKIPPED = "skipped"
WAITING = "waiting"

SKIPPED_KEY = "auto_resume_skipped"
"""Meta key for a decision not to resume. Idempotent, so it needs no exclusive claim."""


@dataclass(frozen=True)
class ResumeDecision:
    """What was decided about one interrupted attempt, for the poller's report."""

    project_id: str
    task_id: str
    finish_id: str
    action: str
    reason: str

    @property
    def subject(self) -> str:
        return self.finish_id

    @property
    def detail(self) -> str:
        return f"{self.task_id}: {self.action} ({self.reason})"


Spawn = Callable[..., Optional[str]]


# ----- finding the attempts worth a look --------------------------------------------


_META_CACHE: Dict[Path, Tuple[int, Dict[str, Any]]] = {}
"""``meta.yaml`` by path, keyed on its mtime. A finished attempt's never changes.

The poll runs every ten seconds against every finish directory within the scan limit, and
YAML parsing is the only part of that which costs anything. A stat per directory is what
remains once this is warm.
"""


def _meta(directory: Path) -> Dict[str, Any]:
    path = directory / "meta.yaml"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _META_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    meta = read_meta(directory)
    _META_CACHE[path] = (stamp, meta)
    return meta


def _written(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - removed mid-scan
        return 0.0


def newest_attempts(home: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """The newest attempt per task, within ``finish_status``'s scan limit."""
    root = finishes_root(home)
    try:
        candidates = [
            entry
            for entry in root.iterdir()
            if entry.is_dir() and entry.name not in (SPAWN_DIRNAME, RECEIPTS_DIRNAME)
        ]
    except OSError:
        return []
    candidates.sort(key=_written, reverse=True)
    newest: Dict[Tuple[str, str], Tuple[Path, Dict[str, Any]]] = {}
    for entry in candidates[:SCAN_LIMIT]:
        meta = _meta(entry)
        task_id = str(meta.get("task_id") or "")
        if not task_id:
            continue
        key = (str(meta.get("project_id") or ""), task_id)
        held = newest.get(key)
        if held is None or str(meta.get("started_at") or "") > str(held[1].get("started_at") or ""):
            newest[key] = (entry, meta)
    return list(newest.values())


def worth_deciding(meta: Mapping[str, Any]) -> bool:
    """Whether this attempt could still need a decision, from its meta alone.

    Cheap on purpose: it is asked of every newest attempt on every tick. An attempt with
    an ending needs nothing -- except a *resumed* one that declined, which wrote an ending
    and handed nothing to anybody.
    """
    if meta.get("authority") not in (APPROVAL, POSTURE):
        return False
    if meta.get(SKIPPED_KEY) or meta.get("auto_resume_parked"):
        return False
    outcome = str(meta.get("outcome") or "")
    if outcome == DECLINED:
        return bool(meta.get("resumed_from"))
    return not meta.get("finished_at") and outcome in ("", "running")


# ----- the claims ---------------------------------------------------------------------


def read_claim(directory: Path, name: str) -> Optional[Dict[str, Any]]:
    """The claim ``name``, or ``None`` when nobody has made it.

    A file that exists but does not parse is a claim -- its writer is between the create
    and the write -- so it reads as an empty one rather than as none.
    """
    try:
        text = (directory / name).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _claim(directory: Path, name: str, payload: Mapping[str, Any]) -> bool:
    """Create ``name`` exclusively. False when somebody already has, or it cannot be written.

    Failing to write is a refusal, not a licence: a resume nothing recorded is exactly the
    one that could be spent twice.
    """
    try:
        handle = os.open(str(directory / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(payload), default=str))
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _started(meta: Mapping[str, Any]) -> datetime:
    try:
        moment = datetime.fromisoformat(str(meta.get("started_at") or ""))
    except ValueError:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ----- deciding -----------------------------------------------------------------------


def decide_attempt(
    home: Path,
    project: Project,
    manager: TaskManagerLike,
    directory: Path,
    meta: Mapping[str, Any],
    *,
    spawn: Optional[Spawn] = None,
) -> Optional[ResumeDecision]:
    """Resume, park, skip or wait on one attempt. ``None`` when it needs nothing. Never raises."""
    try:
        return _decide(home, project, manager, directory, meta, spawn or spawn_finish)
    except Exception as exc:  # noqa: BLE001 - one bad attempt must not stop the poll
        return ResumeDecision(
            project.id,
            str(meta.get("task_id") or ""),
            directory.name,
            WAITING,
            f"could not decide: {type(exc).__name__}: {exc}",
        )


def _decide(
    home: Path,
    project: Project,
    manager: TaskManagerLike,
    directory: Path,
    meta: Mapping[str, Any],
    spawn: Spawn,
) -> Optional[ResumeDecision]:
    task_id = str(meta.get("task_id") or "")
    finish_id = directory.name
    record = FinishDirectory(path=directory, finish_id=finish_id)

    def decision(action: str, reason: str) -> ResumeDecision:
        return ResumeDecision(project.id, task_id, finish_id, action, reason)

    def skip(reason: str) -> ResumeDecision:
        record.write_meta(**{SKIPPED_KEY: reason, "auto_resume_at": _now()})
        return decision(SKIPPED, reason)

    if not worth_deciding(meta) or read_claim(directory, PARK_CLAIM) is not None:
        return None
    resumed = read_claim(directory, RESUME_CLAIM)
    status = read_finish_status(home, task_id, project.id)
    declined = str(meta.get("outcome") or "") == DECLINED
    if not declined:
        # Asked of finish_status rather than re-derived: the lock rule it applies is the one
        # a dispatch refusal applies, and a second rule here would disagree with it.
        if status is None or status.state != INTERRUPTED:
            return None
        if status.directory is not None and status.directory != directory:
            return None
        pid = meta.get("pid")
        if isinstance(pid, int) and process_alive(pid):
            # A posture finish's lock is its run's. A run settled as gone can leave its
            # `agentjobs finish` child running, and a resume beside it is two finishes.
            return decision(WAITING, f"its process {pid} is still running")

    task = manager.get_task(task_id)
    if task is None or not task.is_open:
        return skip("task_closed")
    if not finish_is_offered(project.id, home):
        return decision(WAITING, "this machine does not offer the scripted finish")

    authority = str(meta.get("authority") or "")
    run_id = ""
    if authority == APPROVAL:
        from agentjobs.dispatch.approval import standing_approval_for

        receipt = standing_approval_for(home, task, project.load_config(), project_id=project.id)
        if receipt is None:
            return skip("approval_withdrawn")
        approver = receipt.approver
    else:
        run_id = str(meta.get("run_id") or "")
        if not run_id:
            return skip("no_run")
        try:
            run = find_run(home, run_id)
        except LedgerError:
            return skip("run_missing")
        if run.task_id and run.task_id != task_id:
            return skip("run_is_for_another_task")
        if run.is_live:
            return decision(WAITING, f"run {run_id} is still live")
        if run.outcome == "cancelled":
            return skip("stopped")
        approver = f"run {run_id}"

    guard = AuthorityGuard(
        home=home, project_id=project.id, task_id=task_id, started_at=_started(meta)
    )
    if guard.stop_since_start() is not None:
        return skip("stopped")

    if declined or meta.get("resumed_from") or resumed is not None:
        reason = (
            "resume_declined"
            if declined
            else "resume_never_started"
            if resumed is not None
            else "interrupted_again"
        )
        return _park(record, manager, project, task_id, status, reason, decision)

    if not _claim(directory, RESUME_CLAIM, {"decision": RESUMED, "at": _now(), "by": authority}):
        return None
    record.write_meta(auto_resumed_at=_now())
    spawned = spawn(
        project=project,
        task_id=task_id,
        approver=approver,
        home=home,
        resumed_from=finish_id,
        posture_run_id=run_id,
    )
    if spawned is None:
        return _park(record, manager, project, task_id, status, "resume_not_started", decision)
    return decision(RESUMED, f"on {'the standing approval' if authority == APPROVAL else run_id}")


PARK_REASONS = {
    "interrupted_again": "the attempt started to resume it was interrupted too",
    "resume_declined": "the attempt started to resume it declined without doing anything",
    "resume_never_started": "the attempt started to resume it never began",
    "resume_not_started": "the attempt to resume it could not be started",
}


def _park(
    record: FinishDirectory,
    manager: TaskManagerLike,
    project: Project,
    task_id: str,
    status: Optional[FinishStatus],
    reason: str,
    decision: Callable[[str, str], ResumeDecision],
) -> Optional[ResumeDecision]:
    if not _claim(record.path, PARK_CLAIM, {"decision": PARKED, "at": _now(), "reason": reason}):
        return None
    record.write_meta(auto_resume_parked=reason, auto_resume_at=_now())
    merged = ""
    if status is not None:
        merged = status.merge_commit or status.earlier_merge_commit
    headline = (
        f"**The merge is done: `{merged[:8]}`**; what did not finish is delivery. "
        if merged
        else "**Nothing was merged.** "
    )
    command = f"agentjobs finish {task_id} --project {project.id}"
    manager.handoff(
        task_id,
        actor=FINISHER,
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=(
            f"{headline}The scripted finish `{record.finish_id}` stopped without an ending "
            f"and {PARK_REASONS.get(reason, reason)}, so it is not being resumed again.\n\n"
            "Nothing further will happen to this task until somebody acts. Check the "
            f"finish log on the task page for why, then re-run it:\n\n```\n{command}\n```\n\n"
            "or click Dispatch to put a session on it."
        ),
    )
    return decision(PARKED, reason)


# ----- the two callers ---------------------------------------------------------------


def resume_interrupted_finishes(
    home: Path,
    *,
    registry: Optional[ProjectRegistry] = None,
    managers: Optional[Dict[str, TaskManagerLike]] = None,
    spawn: Optional[Spawn] = None,
) -> List[ResumeDecision]:
    """One pass over every task's newest attempt. What the poll tick calls. Never raises."""
    registry = registry or ProjectRegistry(home=home)
    managers = managers or {}
    decisions: List[ResumeDecision] = []
    for directory, meta in newest_attempts(home):
        if not worth_deciding(meta):
            continue
        project_id = str(meta.get("project_id") or "")
        try:
            project = registry.get(project_id)
        except ProjectError:
            continue
        try:
            manager = managers.get(project_id) or dispatch_manager_for(project)
        except Exception:  # noqa: BLE001 - an unreadable project is not this attempt's fault
            continue
        decided = decide_attempt(home, project, manager, directory, meta, spawn=spawn)
        if decided is not None:
            decisions.append(decided)
    return decisions


def resume_finish_of_settled_run(
    home: Path,
    *,
    project_id: str,
    task_id: str,
    run_id: str,
    manager: TaskManagerLike,
    spawn: Optional[Spawn] = None,
) -> bool:
    """Resume the posture finish ``run_id`` was running when it ended. True if one started.

    Asked by a run's settle path before it writes *nobody was told what this task needs*:
    a run that died inside its own ``agentjobs finish --posture-release`` has told
    somebody -- the finish -- and the finish can carry on. Never raises.
    """
    from agentjobs.dispatch.finish_status import newest_finish_directory

    try:
        directory = newest_finish_directory(home, task_id, project_id)
        if directory is None:
            return False
        meta = _meta(directory)
        if meta.get("authority") != POSTURE or meta.get("run_id") != run_id:
            return False
        project = ProjectRegistry(home=home).get(project_id)
    except Exception:  # noqa: BLE001 - see the docstring
        return False
    decided = decide_attempt(home, project, manager, directory, meta, spawn=spawn)
    return decided is not None and decided.action == RESUMED


__all__ = [
    "PARKED",
    "RESUMED",
    "SKIPPED",
    "WAITING",
    "ResumeDecision",
    "decide_attempt",
    "newest_attempts",
    "resume_finish_of_settled_run",
    "resume_interrupted_finishes",
    "worth_deciding",
]
