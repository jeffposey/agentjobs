"""The replay entry point, source import, outbox delivery and restore reconciliation.

Every function here follows the same two-transaction shape design section 9a prescribes:
decide inside a short transaction from recorded facts, act outside any transaction, and
record the result in a second short transaction that is refused if ownership moved in
between. None of them talks to a task store or a process directly -- the caller hands in
the adapter (a feed reader, a task writer, a reconciler) -- which is what lets the crash
tests drive the real functions against a real journal and a real task store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from agentjobs.execution.errors import ExecutionStoreError, HistoryIncompatible
from agentjobs.execution.reducer import (
    SUPPORTED_VERSIONS,
    Event,
    ExecutionState,
    Intent,
    next_intents,
    replay,
)
from agentjobs.execution.store import (
    Activity,
    Attempt,
    ExecutionStore,
    OutboxItem,
    SourceEvent,
)

MODE_DISABLED = "disabled"
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
CONTROLLER_MODES = (MODE_DISABLED, MODE_SHADOW, MODE_ACTIVE)
"""How far the reducer is trusted to drive work.

Admission, ownership, the terminal compare-and-set and the outbox are authoritative in
every mode -- they are the race fixes (task-264). ``shadow`` records the reducer's
proposals as shadow activities that nothing performs; it is what the journal does for an
execution the legacy poller follows. ``active`` records them for real, for
``dispatch.controller`` to perform through its activity adapters (task-416)."""


@dataclass(frozen=True)
class Advance:
    """What one replay concluded: the state, the proposals, and which were new."""

    state: ExecutionState
    intents: Tuple[Intent, ...]
    recorded: Tuple[str, ...]


def inspect_execution(
    store: ExecutionStore, execution_id: str
) -> Tuple[ExecutionState, Tuple[Intent, ...]]:
    """Replay an execution with no writes at all. The query half of ``advance_execution``."""
    execution = store.execution(execution_id)
    if execution is None:
        raise ExecutionStoreError(f"no execution {execution_id!r}")
    if execution.workflow_version not in SUPPORTED_VERSIONS:
        raise HistoryIncompatible(
            f"execution {execution_id} was written by workflow version "
            f"{execution.workflow_version}; this build replays only {sorted(SUPPORTED_VERSIONS)}"
        )
    events = [
        Event(
            sequence=event.sequence,
            kind=event.kind,
            payload=event.payload,
            source_id=event.source_id,
        )
        for event in store.events(execution_id)
    ]
    state = replay(execution_id, events, version=execution.workflow_version)
    return state, next_intents(state)


def advance_execution(
    store: ExecutionStore, execution_id: str, *, mode: str = MODE_SHADOW
) -> Advance:
    """The single replay entry point for the poller, the CLI and startup recovery.

    Loads the snapshot and the events after it, reduces them, and records each proposed
    intent with its stable activity id. Recording is idempotent, so a second replay of
    the same history records nothing and returns the same intents. In ``shadow`` mode the
    intents are marked shadow and nothing performs them.

    An incompatible history raises before anything is written.
    """
    if mode not in CONTROLLER_MODES:
        raise ValueError(f"unknown controller mode {mode!r}")
    execution = store.execution(execution_id)
    if execution is None:
        raise ExecutionStoreError(f"no execution {execution_id!r}")
    if execution.workflow_version not in SUPPORTED_VERSIONS:
        raise HistoryIncompatible(
            f"execution {execution_id} was written by workflow version "
            f"{execution.workflow_version}; this build replays only {sorted(SUPPORTED_VERSIONS)}. "
            "Nothing was recorded."
        )
    after = execution.snapshot_sequence or 0
    events = [
        Event(
            sequence=event.sequence,
            kind=event.kind,
            payload=event.payload,
            source_id=event.source_id,
        )
        for event in store.events(execution_id, after=after)
    ]
    state = replay(
        execution_id,
        events,
        version=execution.workflow_version,
        snapshot=execution.snapshot if after else None,
    )
    intents = next_intents(state)
    if mode == MODE_DISABLED:
        return Advance(state, intents, ())
    recorded: List[str] = []
    for intent in intents:
        _, created = store.record_intent(
            intent.activity_id,
            execution_id=execution_id,
            kind=intent.kind,
            input=intent.input,
            owner_epoch=execution.controller_epoch,
            run_id=str(intent.input.get("run_id")) if intent.input.get("run_id") else None,
            shadow=mode == MODE_SHADOW,
        )
        if created:
            recorded.append(intent.activity_id)
    if state.last_sequence > after:
        store.save_snapshot(
            execution_id, state.last_sequence, state.as_data(), state_name=state.state
        )
    return Advance(state, intents, tuple(recorded))


# ----- source events: task log -> inbox ----------------------------------------

FeedReader = Callable[[int, int], Sequence[SourceEvent]]
"""``(after_position, limit) -> events``: a task store's bounded feed, oldest first."""


def task_feed_source(project_id: str) -> str:
    """The cursor name for one project's task-log feed."""
    return f"task-log:{project_id}"


def import_source_events(
    store: ExecutionStore,
    project_id: str,
    feed: FeedReader,
    *,
    limit: int = 200,
    max_batches: int = 50,
    not_before: Optional[datetime] = None,
) -> int:
    """Import every unconsumed task-log entry into the inbox. Returns how many were new.

    The cursor advances in the same transaction as the rows it passed, so a process that
    dies after the task store committed a handoff but before this import finishes loses
    nothing: the next import starts at the old cursor. A cursor whose marker no longer
    matches the feed -- a restored task database renumbers it -- is rewound to the start,
    and the inbox's unique source ids make the re-import a no-op for everything already
    seen.
    """
    source = task_feed_source(project_id)
    cursor = store.cursor(source)
    if cursor.position and cursor.marker:
        check = list(feed(cursor.position - 1, 1))
        if not check or check[0].marker != cursor.marker:
            store.reset_cursor(source, reason="feed marker no longer matches; restored store")
            cursor = store.cursor(source)
    total = 0
    position = cursor.position
    for _ in range(max_batches):
        batch = list(feed(position, limit))
        if not batch:
            break
        total += store.import_source_events(source, batch, not_before=not_before)
        position = max(event.position for event in batch)
        if len(batch) < limit:
            break
    return total


# ----- outbox: journal -> task store --------------------------------------------

OutboxApplier = Callable[[OutboxItem], Mapping[str, object]]
"""Performs one owed task-store write using ``item.operation_id`` and returns a receipt.

Must be idempotent on that operation id -- the task manager's operation ledger is what
makes it so -- because a crash after the write and before the acknowledgement delivers
the same item again."""


@dataclass(frozen=True)
class Delivery:
    operation_id: str
    delivered: bool
    detail: str


def deliver(
    store: ExecutionStore, item: OutboxItem, apply: OutboxApplier, *, final: bool = False
) -> Delivery:
    """Deliver one outbox item and acknowledge it. The acknowledgement is its own commit."""
    try:
        receipt = apply(item)
    except Exception as exc:  # noqa: BLE001 - recorded on the item; the next flush retries
        store.record_outbox_failure(
            item.operation_id, error=f"{type(exc).__name__}: {exc}", final=final
        )
        return Delivery(item.operation_id, False, str(exc))
    store.acknowledge(item.operation_id, receipt=dict(receipt))
    return Delivery(item.operation_id, True, "delivered")


def flush_outbox(
    store: ExecutionStore,
    apply: OutboxApplier,
    *,
    project_id: Optional[str] = None,
) -> List[Delivery]:
    """Deliver every pending item, oldest first."""
    return [deliver(store, item, apply) for item in store.pending_outbox(project_id=project_id)]


# ----- releasing what nothing holds any more ------------------------------------

AttemptEvidence = Callable[[Attempt], Optional[Tuple[str, str, str]]]
"""``attempt -> (outcome, status, reason)`` when evidence the adapter trusts shows the
attempt is over, else ``None``. What counts as evidence is the adapter's contract; the
dispatch adapter's is documented in ``dispatch.journal.attempt_evidence``."""


def release_ended_attempts(
    store: ExecutionStore,
    evidence: AttemptEvidence,
    *,
    project_id: Optional[str] = None,
    concluded_by: str = "reconcile",
) -> List[Attempt]:
    """Conclude live attempts that evidence shows ended. Returns those this call concluded.

    A busy or failing journal raises out of here having released nothing, which is the
    required direction: an attempt is never freed on the strength of a transition that
    did not commit.
    """
    released: List[Attempt] = []
    for attempt in store.live_attempts(project_id=project_id):
        verdict = evidence(attempt)
        if verdict is None:
            continue
        outcome, status, reason = verdict
        conclusion = store.conclude(
            attempt.run_id,
            outcome=outcome,
            status=status,
            concluded_by=f"{concluded_by}: {reason}"[:300],
            epoch=attempt.epoch,
        )
        if conclusion.won:
            released.append(conclusion.attempt)
    return released


# ----- restore ------------------------------------------------------------------

ActivityReconciler = Callable[[Activity], Tuple[str, Optional[Mapping[str, object]]]]
"""``activity -> (state, result)`` where state is ``applied``, ``not_applied``,
``still_running`` or ``unknown``, established from the world rather than the journal."""


@dataclass(frozen=True)
class RestoreReport:
    activities: Tuple[Tuple[str, str], ...]
    released: Tuple[str, ...]
    outbox_pending: Tuple[str, ...]


def reconcile_after_restore(
    store: ExecutionStore,
    *,
    reconcile_activity: ActivityReconciler,
    evidence: AttemptEvidence,
) -> RestoreReport:
    """Bring a restored journal back into agreement with the world before anything acts.

    Every effect whose intent the snapshot holds without a result is asked of the world:
    it may have happened after the snapshot was taken. The answer is recorded as the
    activity's state -- ``unknown`` stays ``unknown`` and is never quietly downgraded to
    ``not_applied``. Attempts the snapshot believes live are concluded only on evidence.
    Owed task writes stay pending; their operation ids make the redelivery safe.
    """
    reconciled: List[Tuple[str, str]] = []
    for activity in store.in_flight_activities():
        state, result = reconcile_activity(activity)
        if state not in {"applied", "not_applied", "still_running", "unknown"}:
            raise ValueError(f"reconciler returned {state!r} for {activity.activity_id}")
        execution = store.execution(activity.execution_id)
        epoch = execution.controller_epoch if execution is not None else activity.owner_epoch
        store.record_result(
            activity.activity_id,
            state=state,
            owner_epoch=epoch,
            result=result,
            error_class="effect_unknown" if state == "unknown" else None,
        )
        reconciled.append((activity.activity_id, state))
    released = release_ended_attempts(store, evidence, concluded_by="restore")
    return RestoreReport(
        activities=tuple(reconciled),
        released=tuple(attempt.run_id for attempt in released),
        outbox_pending=tuple(item.operation_id for item in store.pending_outbox()),
    )


__all__ = [
    "Advance",
    "CONTROLLER_MODES",
    "Delivery",
    "MODE_ACTIVE",
    "MODE_DISABLED",
    "MODE_SHADOW",
    "RestoreReport",
    "advance_execution",
    "deliver",
    "flush_outbox",
    "import_source_events",
    "inspect_execution",
    "reconcile_after_restore",
    "release_ended_attempts",
    "task_feed_source",
]
