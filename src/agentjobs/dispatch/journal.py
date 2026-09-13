"""Dispatch's adapter onto the execution journal (task-264).

``agentjobs.execution`` knows nothing about runs, run directories or task managers; this
module is where those meet it. Every dispatch path that decides *who owns a task*, *whether
a slot is free*, *whether a cancellation was asked for* or *who writes a run's terminal
result* asks here, and the answer comes from the journal rather than from a run's
``meta.yaml``.

**Why not the meta file.** It was the authority until now, and three defects on task-264
are what that cost. Two unlocked read-modify-writes of it -- a poll and a cancellation --
both believed they concluded one run (task-107's ``cancelled`` then ``interrupted``). A
directory scan counted slots seconds before the directories it counted existed, so the
machine ceiling had no primitive at all. And the file is writable by the agent working
inside the run directory, so anything that trusted ``status:`` from it let a run declare
itself over and slip the live-run lock (auditor 12). The file is still written, and every
surface still reads it; it is a projection of what the journal decided.

**Legacy runs.** A run started before this build has a directory and no journal row. It
is adopted as ``legacy`` the first time something needs to act on it, and until then every
check falls back to its meta exactly as before -- an upgrade under live runs neither
ignores them nor counts them twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from agentjobs.execution.coordinator import release_ended_attempts
from agentjobs.execution.errors import ExecutionStoreError, OwnershipConflict
from agentjobs.execution.factory import execution_store_for
from agentjobs.execution.reducer import WORKFLOW_VERSION
from agentjobs.execution.store import (
    PROVENANCE_LEGACY,
    Attempt,
    Conclusion,
    ExecutionStore,
    OutboxItem,
    SourceEvent,
)
from agentjobs.models_v2 import DispatchOutcome

if TYPE_CHECKING:  # pragma: no cover - ledger and runner import this module
    from agentjobs.dispatch.ledger import RunRecord
    from agentjobs.store_factory import TaskManagerLike

OPERATION_NAMESPACE = uuid.UUID("6f0e4b0c-2640-4a5e-9d11-6a6f75726e6c")
"""Namespace for the task writes the journal owes. A uuid5 under it is a stable operation
id: the same run's result always carries the same id, so a replay after a lost
acknowledgement is recognised by the task manager's operation ledger as the same write."""

TERMINAL_OUTCOME_STATUS: Dict[str, str] = {
    DispatchOutcome.CANCELLED.value: "cancelled",
    DispatchOutcome.INTERRUPTED.value: "failed",
    DispatchOutcome.CRASHED.value: "failed",
}
"""The ``status`` a concluded run's meta carries, by outcome.

Until task-264 ``DispatchLedger._conclude`` wrote ``cancelled`` for every outcome it
recorded, including ``interrupted`` -- so a run swept at startup read as cancelled by
somebody, which nobody did. Everything not named here ends ``finished``, which is what the
runner's own settle paths have always written."""


def status_for(outcome: DispatchOutcome) -> str:
    return TERMINAL_OUTCOME_STATUS.get(outcome.value, "finished")


def journal(home: Path) -> ExecutionStore:
    """The machine's execution journal."""
    return execution_store_for(Path(home))


def operation_id(kind: str, project_id: str, task_id: str, run_id: str) -> str:
    """The stable operation id for one owed task write about one run."""
    return str(uuid.uuid5(OPERATION_NAMESPACE, f"{kind}:{project_id}:{task_id}:{run_id}"))


def result_operation_id(project_id: str, task_id: str, run_id: str) -> str:
    return operation_id("dispatch_result", project_id, task_id, run_id)


# ----- legacy adoption ----------------------------------------------------------


def ensure_attempt(home: Path, record: "RunRecord") -> Attempt:
    """The journal's row for a run, adopting a pre-journal run as ``legacy`` if needed."""
    store = journal(home)
    found = store.attempt(record.run_id)
    if found is not None:
        return found
    try:
        return store.adopt_legacy_attempt(
            run_id=record.run_id,
            project_id=record.project_id,
            task_id=record.task_id,
            mode=record.mode,
            takes_slot=record.takes_slot,
            live=record.is_live,
            session_id=record.session_id,
            admitted_at=record.started_at.isoformat() if record.started_at else None,
            status=record.status,
            outcome=record.outcome,
        )
    except OwnershipConflict:
        # Another live attempt already owns the task. The legacy run cannot be live
        # beside it, so it is recorded as the history it is.
        adopted = store.attempt(record.run_id)
        if adopted is not None:
            return adopted
        return store.adopt_legacy_attempt(
            run_id=record.run_id,
            project_id=record.project_id,
            task_id=record.task_id,
            mode=record.mode,
            takes_slot=record.takes_slot,
            live=False,
            session_id=record.session_id,
            status=record.status,
            outcome=record.outcome,
        )


def journal_liveness(home: Path, run_id: str) -> Optional[bool]:
    """Whether the journal says a run is live, or ``None`` when it has no row (or cannot
    be read). ``None`` sends the caller to its pre-journal evidence."""
    try:
        attempt = journal(home).attempt(run_id)
    except ExecutionStoreError:
        return None
    if attempt is None:
        return None
    return attempt.is_live


# ----- cancellation and conclusion ------------------------------------------------


def request_cancel(
    home: Path, record: "RunRecord", *, requester: str, source: str, reason: str = ""
) -> Attempt:
    """Record who asked for this run to stop, before anything is signalled."""
    ensure_attempt(home, record)
    return journal(home).request_cancel(
        record.run_id, requester=requester, source=source, reason=reason
    )


def cancel_requested(
    home: Path, run_id: str, *, meta: Optional[Mapping[str, object]] = None
) -> bool:
    """Whether a Stop is on record for this run.

    The journal is the authority for every run it knows. The meta flag is read only for
    a run it has no row for -- a cancellation a pre-journal process requested -- so an
    agent writing the flag into its own meta cannot hold its run open against the poller.
    """
    try:
        attempt = journal(home).attempt(run_id)
    except ExecutionStoreError:
        attempt = None
    if attempt is not None:
        return attempt.cancel_requested
    return bool(meta is not None and meta.get("cancel_requested"))


def claim_conclusion(
    home: Path,
    record: "RunRecord",
    outcome: DispatchOutcome,
    *,
    concluded_by: str,
    status: Optional[str] = None,
    projection: Optional[OutboxItem] = None,
) -> Conclusion:
    """Ask for the one right to write this run's terminal result.

    A journal that cannot be written raises: a conclusion that did not commit must not be
    acted on as though it had. Callers on a supervision path catch ``ExecutionStoreError``
    and leave the run live for the next pass, which is the recoverable direction.
    """
    ensure_attempt(home, record)
    return journal(home).conclude(
        record.run_id,
        outcome=outcome.value,
        status=status or status_for(outcome),
        concluded_by=concluded_by,
        projection=projection,
    )


def result_projection(
    record: "RunRecord",
    outcome: DispatchOutcome,
    *,
    actor: str,
    re: Optional[int],
    duration_seconds: Optional[float],
    body: Optional[str],
    exit_code: Optional[int] = None,
) -> OutboxItem:
    """The ``dispatch_result`` a conclusion owes its task, as an outbox item."""
    payload: Dict[str, object] = {
        "actor": actor,
        "run_id": record.run_id,
        "outcome": outcome.value,
        "re": re,
        "duration_seconds": duration_seconds,
        "log_path": str(record.path),
        "body": body,
        "exit_code": exit_code,
    }
    return OutboxItem(
        operation_id=result_operation_id(record.project_id, record.task_id, record.run_id),
        project_id=record.project_id,
        task_id=record.task_id,
        kind="dispatch_result",
        payload=payload,
        run_id=record.run_id,
    )


def apply_projection(manager: "TaskManagerLike", item: OutboxItem) -> Dict[str, object]:
    """Perform one owed task write. Idempotent on the item's operation id."""
    if item.kind != "dispatch_result":
        raise ValueError(f"no applier for outbox kind {item.kind!r}")
    from agentjobs.manager import DuplicateDispatchResultError

    payload = item.payload
    try:
        task = manager.record_dispatch_result(
            item.task_id,
            actor=str(payload["actor"]),
            run_id=str(payload["run_id"]),
            outcome=DispatchOutcome(str(payload["outcome"])),
            re=payload.get("re"),
            exit_code=payload.get("exit_code"),
            duration_seconds=payload.get("duration_seconds"),
            log_path=payload.get("log_path"),
            body=payload.get("body"),
            operation_id=item.operation_id,
        )
    except DuplicateDispatchResultError as exc:
        # The task already records this run's ending, written by a path that did not carry
        # this operation id. That is the fact the item owed, so it is acknowledged rather
        # than retried against a refusal that will never change.
        return {"already_recorded": str(exc)}
    entry_id = next(
        (
            entry.id
            for entry in reversed(task.log)
            if entry.type.value == "dispatch_result" and entry.data.get("run_id") == item.run_id
        ),
        None,
    )
    return {"entry_id": entry_id}


def deliver_projection(home: Path, manager: "TaskManagerLike", item: OutboxItem) -> bool:
    """Deliver one owed write now, acknowledging it. False leaves it pending for a sweep."""
    from agentjobs.execution.coordinator import deliver

    return deliver(journal(home), item, lambda owed: apply_projection(manager, owed)).delivered


def flush_owed_results(
    home: Path, resolve_manager: Callable[[str], Optional["TaskManagerLike"]]
) -> List[str]:
    """Deliver every owed terminal result whose project resolves. Returns delivered ids.

    What makes a crash between a conclusion's commit and its task write recoverable: the
    write is in the outbox, and this sweep (startup reconcile) delivers it with the same
    operation id, so a write that did land before the crash is replayed rather than
    duplicated.
    """
    from agentjobs.execution.coordinator import deliver

    store = journal(home)
    delivered: List[str] = []
    for item in store.pending_outbox():
        manager = resolve_manager(item.project_id)
        if manager is None:
            continue
        if deliver(store, item, lambda owed: apply_projection(manager, owed)).delivered:
            delivered.append(item.operation_id)
    return delivered


# ----- admission ------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyView:
    """What the run directories say that the journal does not know yet."""

    slot_holders: Tuple[str, ...]
    owners: Tuple[str, ...]
    recent_starts: Tuple[str, ...]


def legacy_view(
    home: Path, *, project_id: str, task_id: str, now: Optional[datetime] = None
) -> LegacyView:
    """Run ids from directories, for runs the journal has no row for.

    Read outside any transaction; see ``ExecutionStore.admit`` for why that is safe.
    """
    from agentjobs.dispatch.ledger import list_runs

    store = journal(home)
    moment = now or datetime.now(timezone.utc)
    hour_ago = moment - timedelta(hours=1)
    slots: List[str] = []
    owners: List[str] = []
    recent: List[str] = []
    for record in list_runs(home):
        if store.attempt(record.run_id) is not None:
            continue
        if record.started_at is not None and record.started_at >= hour_ago:
            recent.append(record.run_id)
        if not record.is_live:
            continue
        if record.takes_slot:
            slots.append(record.run_id)
        if record.task_id == task_id and record.project_id in (project_id, ""):
            owners.append(record.run_id)
    return LegacyView(tuple(slots), tuple(owners), tuple(recent))


def attempt_evidence(
    home: Path,
    resolve_manager: Callable[[str], Optional["TaskManagerLike"]],
    *,
    process_alive: Optional[Callable[[int], bool]] = None,
) -> Callable[[Attempt], Optional[Tuple[str, str, str]]]:
    """What counts as proof that an attempt the journal thinks is live has ended.

    Four kinds, and what is *absent* is the point -- a ``status:`` a worker wrote into its
    own meta is never enough for a run AgentJobs started:

    1. **A ``dispatch_result`` for the run on its task.** Only the manager's dispatch verb
       writes that entry type (``add_log_entry`` refuses it), so no agent can forge one.
    2. **Admitted, never launched, and the admitting process is gone** -- and the run
       directory names no session and no pid. A launcher that died after spawning is
       *not* this case: its directory names what it spawned, and the attempt stays owned
       until something reconciles that worker.
    3. **A session a person was sitting in, or one that registered itself**, whose meta
       reads terminal. Its meta is the person's or the session's own record and grants
       nothing to anyone else.
    4. **A legacy-imported run whose meta reads terminal** -- the only evidence that ever
       existed for it.
    """
    from agentjobs.dispatch.ledger import (
        process_alive as default_alive,
        read_run,
    )
    from agentjobs.dispatch.runner import runs_root

    alive = process_alive or default_alive

    def verdict(attempt: Attempt) -> Optional[Tuple[str, str, str]]:
        directory = runs_root(home) / attempt.run_id
        record = read_run(directory) if directory.is_dir() else None
        manager = resolve_manager(attempt.project_id)
        if manager is not None:
            try:
                task = manager.get_task(attempt.task_id)
            except Exception:  # noqa: BLE001 - an unreadable task proves nothing
                task = None
            if task is not None:
                for entry in reversed(task.log):
                    if (
                        entry.type.value == "dispatch_result"
                        and entry.data.get("run_id") == attempt.run_id
                    ):
                        outcome = str(entry.data.get("outcome") or "")
                        try:
                            status = status_for(DispatchOutcome(outcome))
                        except ValueError:
                            status = "finished"
                        return outcome, status, "its dispatch_result is on the task"
        if attempt.state == "admitted" and attempt.holder_pid is not None:
            launched = record is not None and (record.session_id or record.pid is not None)
            if not launched and not alive(int(attempt.holder_pid)):
                return (
                    DispatchOutcome.CRASHED.value,
                    "failed",
                    "admitted but never launched, and the process that admitted it is gone",
                )
        if record is not None and not record.is_live:
            if record.is_interactive or record.origin:
                return (
                    record.outcome or DispatchOutcome.SESSION_ENDED.value,
                    record.status,
                    "a person's or a registered session's own record says it ended",
                )
            if attempt.provenance == PROVENANCE_LEGACY:
                return (
                    record.outcome or DispatchOutcome.INTERRUPTED.value,
                    record.status,
                    "a pre-journal run whose record says it ended",
                )
        return None

    return verdict


def release_ended(
    home: Path, resolve_manager: Callable[[str], Optional["TaskManagerLike"]]
) -> List[Attempt]:
    """Conclude live attempts that evidence shows are over. Never raises for a busy journal."""
    try:
        return release_ended_attempts(journal(home), attempt_evidence(home, resolve_manager))
    except ExecutionStoreError:
        return []


def admit_dispatch(
    home: Path,
    *,
    project_id: str,
    task_id: str,
    run_id: str,
    capacity: int,
    hourly_limit: Optional[int],
    envelope: Mapping[str, object],
    mode: str,
    resolve_manager: Callable[[str], Optional["TaskManagerLike"]],
    reservation: Optional[Mapping[str, object]] = None,
) -> Attempt:
    """Admit one dispatch: ownership, slot and reservation in one transaction.

    Attempts that evidence shows have ended are released first, so a crash that left a
    row behind costs one reconciliation rather than a permanently occupied slot. Raises the
    store's ``OwnershipConflict`` or ``CapacityExhausted`` for the caller to turn into
    the refusal a person reads.
    """
    release_ended(home, resolve_manager)
    legacy = legacy_view(home, project_id=project_id, task_id=task_id)
    return journal(home).admit(
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        capacity=capacity,
        hourly_limit=hourly_limit,
        mode=mode,
        envelope=envelope,
        workflow_version=WORKFLOW_VERSION,
        operation_id=operation_id("admission", project_id, task_id, run_id),
        legacy_slot_holders=legacy.slot_holders,
        legacy_owners=legacy.owners,
        legacy_recent_starts=legacy.recent_starts,
        reservation=reservation,
    )


def abandon_admission(home: Path, run_id: str, *, launched: bool, reason: str) -> None:
    """Conclude an attempt whose dispatch raised before it could hand back a run.

    ``launched`` decides the reservation. Nothing started means nothing was paid for, so
    the reservation is refunded; a run directory on disk means a launch was at least
    attempted and may have spent money, and an ambiguous launch never refunds.
    """
    try:
        journal(home).conclude(
            run_id,
            outcome=DispatchOutcome.CRASHED.value,
            status="failed",
            concluded_by=f"dispatch raised before handing back a run: {reason}"[:300],
            refund=not launched,
        )
    except ExecutionStoreError:
        return


def mark_launched(home: Path, run_id: str, *, session_id: Optional[str] = None) -> Optional[str]:
    """Record that a run's worker exists. Returns an error string instead of raising.

    A journal failure here must not stop a launched worker being followed: its meta names
    it, and the attempt stays ``admitted`` -- which ``attempt_evidence`` will not release
    while the directory names a session or a pid.
    """
    try:
        journal(home).mark_launched(run_id, session_id=session_id)
    except ExecutionStoreError as exc:
        return str(exc)
    return None


# ----- cross-project identity ------------------------------------------------------


class _TaskScoped(Protocol):
    @property
    def task_id(self) -> str:
        ...

    @property
    def project_id(self) -> str:
        ...


def same_task(record: _TaskScoped, project_id: str, task_id: str) -> bool:
    """Whether a run belongs to this project's task.

    A record with no project id (hand-written, or older than the field) matches any
    project: for a refusal that is the direction that cannot put two runs on one task.
    Lookups that *act on* a run -- wake, reap -- use ``strictly_same_task`` instead.
    """
    return record.task_id == task_id and record.project_id in (project_id, "")


def strictly_same_task(record: _TaskScoped, project_id: str, task_id: str) -> bool:
    """Whether a run is provably this project's task. For lookups that act on the run."""
    return record.task_id == task_id and record.project_id == project_id


def effective_live(home: Path, records: Sequence["RunRecord"]) -> List["RunRecord"]:
    """Filter directory-live runs by the journal where it has an opinion."""
    kept: List["RunRecord"] = []
    for record in records:
        liveness = journal_liveness(home, record.run_id)
        if liveness is None:
            if record.is_live:
                kept.append(record)
        elif liveness:
            kept.append(record)
    return kept


# ----- the source feed and the shadow controller ------------------------------------


def task_feed(manager: "TaskManagerLike") -> Callable[[int, int], Sequence[SourceEvent]]:
    """A project's task-log feed, in the shape the coordinator imports."""

    def read(after: int, limit: int) -> Sequence[SourceEvent]:
        rows = manager.source_events(after, limit)
        return [
            SourceEvent(
                position=int(row["position"]),
                project_id=str(row["project_id"]),
                task_id=str(row["task_id"]),
                entry_id=int(row["entry_id"]),
                ts=str(row["ts"]),
                type=str(row["type"]),
                actor=str(row["actor"]),
                data=dict(row.get("data") or {}),
            )
            for row in rows
        ]

    return read


@dataclass(frozen=True)
class ShadowReport:
    """What one shadow pass did, for the poller's report line."""

    imported: int
    advanced: Tuple[str, ...]
    errors: Tuple[str, ...]


def shadow_tick(
    home: Path, resolve_manager: Callable[[str], Optional["TaskManagerLike"]]
) -> ShadowReport:
    """Import every project's unseen task-log entries, then replay each open execution.

    Shadow mode (task-264): the replay records its proposals and performs none of them.
    What this pass does do for real is keep the inbox current, so a handoff a process
    committed and died before announcing is in the journal within one poll. Never raises;
    a failure becomes a line in the report.
    """
    from agentjobs.execution.coordinator import (
        MODE_SHADOW,
        advance_execution,
        import_source_events,
    )

    errors: List[str] = []
    imported = 0
    advanced: List[str] = []
    try:
        store = journal(home)
        open_executions = store.executions(open_only=True)
    except ExecutionStoreError as exc:
        return ShadowReport(0, (), (f"journal unreadable: {exc}",))
    for project_id in sorted({execution.project_id for execution in open_executions}):
        manager = resolve_manager(project_id)
        if manager is None or not hasattr(manager, "source_events"):
            continue
        try:
            imported += import_source_events(store, project_id, task_feed(manager))
        except Exception as exc:  # noqa: BLE001 - reported, and the next tick retries
            errors.append(f"{project_id}: feed import failed: {exc}")
    for execution in open_executions:
        try:
            result = advance_execution(store, execution.execution_id, mode=MODE_SHADOW)
        except ExecutionStoreError as exc:
            errors.append(f"{execution.execution_id}: {exc}")
            continue
        if result.recorded:
            advanced.append(execution.execution_id)
    return ShadowReport(imported, tuple(advanced), tuple(errors))


# ----- legacy migration --------------------------------------------------------------

LEGACY_ENVELOPE_FIELDS = (
    "runner",
    "driver",
    "mode",
    "group",
    "selection_source",
    "posture",
    "posture_source",
    "posture_ceiling",
    "posture_requested",
    "agent",
    "trigger",
)
"""The envelope fields a legacy import looks for in a run's meta.

Only what the directory recorded is carried. Anything absent is listed as unknown and
stays unknown -- never reconstructed from the project's defaults of today, which is the
silent-downgrade failure task-410's resume already demonstrated."""


@dataclass(frozen=True)
class LegacyImport:
    run_id: str
    disposition: str
    unknown_fields: Tuple[str, ...]


def migrate_legacy_runs(home: Path, *, dry_run: bool = False) -> List[LegacyImport]:
    """Import every run directory the journal does not know yet, explicitly and repeatably.

    Run it once after upgrading, or again whenever; a second run imports nothing new. A
    live legacy run is imported under the ``legacy`` controller, so no durable controller
    can claim it while the code that started it still follows it. ``dry_run`` reports what
    would be imported and writes nothing.
    """
    from agentjobs.dispatch.ledger import list_runs

    store = journal(home)
    results: List[LegacyImport] = []
    for record in reversed(list_runs(home)):
        meta = _read_meta(record)
        envelope = {key: meta[key] for key in LEGACY_ENVELOPE_FIELDS if meta.get(key)}
        unknown = tuple(key for key in LEGACY_ENVELOPE_FIELDS if not meta.get(key))
        if not record.project_id or not record.task_id:
            results.append(LegacyImport(record.run_id, "unattributable", unknown))
            continue
        existing = store.attempt(record.run_id)
        if existing is not None and existing.execution_id:
            results.append(LegacyImport(record.run_id, "already", unknown))
            continue
        if dry_run:
            results.append(LegacyImport(record.run_id, "would_import", unknown))
            continue
        disposition, _ = store.import_legacy_run(
            run_id=record.run_id,
            project_id=record.project_id,
            task_id=record.task_id,
            mode=record.mode,
            takes_slot=record.takes_slot,
            live=record.is_live,
            session_id=record.session_id,
            admitted_at=record.started_at.isoformat() if record.started_at else None,
            status=record.status,
            outcome=record.outcome,
            envelope={"legacy_meta": envelope},
            unknown_fields=unknown,
            workflow_version=WORKFLOW_VERSION,
        )
        results.append(LegacyImport(record.run_id, disposition, unknown))
    return results


def _read_meta(record: "RunRecord") -> Dict[str, object]:
    from agentjobs.dispatch.atomic_yaml import read_yaml_resiliently
    from agentjobs.dispatch.runner import META_FILENAME

    loaded = read_yaml_resiliently(record.path / META_FILENAME)
    return dict(loaded) if isinstance(loaded, dict) else {}


__all__ = [
    "LEGACY_ENVELOPE_FIELDS",
    "LegacyImport",
    "LegacyView",
    "ShadowReport",
    "migrate_legacy_runs",
    "shadow_tick",
    "task_feed",
    "OPERATION_NAMESPACE",
    "abandon_admission",
    "admit_dispatch",
    "apply_projection",
    "attempt_evidence",
    "cancel_requested",
    "claim_conclusion",
    "deliver_projection",
    "effective_live",
    "ensure_attempt",
    "flush_owed_results",
    "journal",
    "journal_liveness",
    "legacy_view",
    "mark_launched",
    "operation_id",
    "release_ended",
    "request_cancel",
    "result_operation_id",
    "result_projection",
    "same_task",
    "status_for",
    "strictly_same_task",
]
