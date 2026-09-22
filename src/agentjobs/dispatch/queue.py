"""The machine's dispatch queue: an authorised dispatch that waits for a slot (task-459).

Design section 7 said **refuse rather than queue** from task-191 until 2026-09-18, and the
two reasons it gave were good ones. They are answered rather than dropped, and the
answers are what this module is:

* *"A queue is a promise to spend money at a moment nobody is watching."* The promise is
  now explicit -- a caller has to send ``if_full: queue``, and nothing enqueues on a
  caller's behalf -- it is **visible**, on the slot board with its own rail, it is
  **cancellable** from the same control that cancels a run, and it is **bounded** by
  ``limits.dispatch_queue_limit``. The §7 spend caps bind a queued start exactly as they
  bind a click, and ``dispatches_per_hour`` counts it when it *starts*, which is when the
  money is spent.
* *"The spawn-time gates would be judged with nobody present."* They are judged at start
  time, by the same function, with nobody's word taken for any of them:
  :func:`start_due` calls ``guards.dispatch_task`` whole. ``require_clean_tree``,
  ``claim_lost``, ``owner_mismatch``, the live-run-per-task rule, the kill switch, the
  ceiling and every cap run *then*, against the world as it is then. A gate that refuses
  permanently takes the entry out of the queue and writes why on the task, so nothing
  waits forever behind a condition that will not clear.

**The queue holds no execution and no run.** An execution begins at admission and this
row exists precisely because admission has not happened; there is nothing here to replay
and nothing to recover. What it holds is one sentence -- *this person authorised this
dispatch and asked for it to start when the machine has room* -- durably, so a server
restart finds it where it left it.

**Why it is not a second waiter.** The controller (task-416) already holds a *continued*
execution in ``capacity_wait``: a timer, a policy observation at the moment it fires, and
a relaunch through ``dispatch_task``. This is that shape one step earlier, applied to a
first admission, and it runs inside the same tick -- see
:meth:`agentjobs.dispatch.controller.Controller.start_queued`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.address import probe_api_base
from agentjobs.dispatch.budget import DISPATCHER_ACTOR, check_budget, check_machine_budget
from agentjobs.dispatch.config import (
    DispatchError,
    DispatchLimits,
    Posture,
    machine_ceiling,
)
from agentjobs.dispatch.guards import (
    IF_FULL_QUEUE,
    RunHandle,
    AlreadyQueuedError,
    ConcurrencyLimitError,
    DispatchQueueFullError,
    DispatchRefused,
    DispatchRequest,
    dispatch_task,
    effective_live_runs,
    live_runs,
)
from agentjobs.dispatch import start_pause
from agentjobs.dispatch.journal import journal
from agentjobs.execution.errors import (
    AlreadyQueued,
    ExecutionStoreError,
    QueueFull,
)
from agentjobs.execution.store import (
    QUEUE_REFUSED,
    QUEUE_STARTED,
    ExecutionStore,
    QueuedDispatch,
)
from agentjobs.models_v2 import DispatchTrigger, LogEntryType
from agentjobs.playbooks.pointer import PlaybookPointer
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

RETRY_AFTER_SECONDS = 30
"""How long an entry that was tried and put back waits before being tried again.

The tick runs every few seconds; the conditions that put an entry back -- the task's own
run still finishing, a hold somebody has to lift -- clear on a human scale. Thirty
seconds is short enough that nobody watching the board sees a stale queue and long enough
that an unstartable entry is not re-judged sixty times a minute.
"""

SOURCE_MANUAL = "manual"
"""A person asked for this one. Manual entries sort ahead of everything else queued at
the same moment simply by being queued first, which is all FIFO needs -- see
:func:`start_due` for why the order is arrival and not the task queue's own."""

SOURCE_PULL = "pull"
"""Reserved for the pull mode (a sibling task). Nothing writes it here; the field exists
so that when something does, the board and the ledger can already say which is which."""

TRANSIENT_CLASSES = (
    "concurrency_limit",
    "machine_per_hour",
    "cooldown",
    "live_run_exists",
    "not_configured",
    "disabled",
    "sentinel",
    "project_not_enabled",
    "task_on_hold",
)
"""Refusals at start time that clear on their own, so the entry keeps its place.

Everything not in this tuple takes the entry out of the queue with the refusal written on
the task. The list is short and deliberately conservative: a condition that a person has
to go and change -- a dirty tree, a lost claim, a closed task, a runner that no longer
exists -- is one nobody would learn about from an entry that silently kept waiting.
"""


@dataclass(frozen=True)
class QueueDecision:
    """What one pass over the queue did to one entry, for the tick's report."""

    queue_id: str
    task_id: str
    project_id: str
    outcome: str
    """``started``, ``waiting``, ``refused`` or ``unresolvable``."""
    detail: str
    run_id: Optional[str] = None

    def describe(self) -> str:
        run = f" as {self.run_id}" if self.run_id else ""
        return f"{self.task_id} {self.outcome}{run}: {self.detail}"


# ----- the request, stored and rebuilt -----------------------------------------


def request_payload(request: DispatchRequest) -> Dict[str, Any]:
    """The parts of a dispatch request a later start has to reproduce.

    Everything here is *what was asked for*, never what was granted: no runner
    resolution, no posture decision, no authorising entry id that does not already exist.
    The grant is made at start time, by the gates, which is the whole argument for
    queueing being safe at all.
    """
    return {
        "task_id": request.task_id,
        "caused_by": request.caused_by,
        "trigger": request.trigger.value,
        "runner": request.runner,
        "group": request.group,
        "authorized_by": request.authorized_by,
        "authorization_note": request.authorization_note,
        "surface": request.surface,
        "posture": request.posture.value if request.posture is not None else None,
        "on_behalf_of_parent": request.on_behalf_of_parent,
        "playbook": (
            {
                "name": request.playbook.name,
                "path": request.playbook.path,
                "digest": request.playbook.digest,
            }
            if request.playbook is not None
            else None
        ),
    }


def rebuild_request(entry: QueuedDispatch) -> DispatchRequest:
    """The stored ask, as a fresh :class:`DispatchRequest` for ``dispatch_task``.

    ``if_full`` is deliberately not carried across. At start time a full machine means
    *keep waiting*, which is this module's job and not a field on the request; rebuilding
    it as ``queue`` would ask ``dispatch_or_queue`` to enqueue a second entry for a task
    that already has one.

    ``admission_operation_id`` is derived from the queue id, so a start that commits the
    admission and then loses the answer -- a crash, a killed server -- finds that same
    attempt on the retry rather than paying for a second one.
    """
    stored = entry.request
    playbook = stored.get("playbook")
    return DispatchRequest(
        task_id=entry.task_id,
        caused_by=stored.get("caused_by"),
        trigger=DispatchTrigger(stored.get("trigger") or DispatchTrigger.MANUAL.value),
        runner=stored.get("runner"),
        group=stored.get("group"),
        authorized_by=stored.get("authorized_by"),
        authorization_note=stored.get("authorization_note"),
        surface=stored.get("surface"),
        posture=Posture(stored["posture"]) if stored.get("posture") else None,
        on_behalf_of_parent=bool(stored.get("on_behalf_of_parent")),
        playbook=(
            PlaybookPointer(
                name=str(playbook.get("name") or ""),
                path=str(playbook.get("path") or ""),
                digest=str(playbook.get("digest") or ""),
            )
            if isinstance(playbook, Mapping)
            else None
        ),
        admission_operation_id=f"queue:{entry.queue_id}",
    )


# ----- enqueueing ---------------------------------------------------------------


def machine_limits(home: Path) -> DispatchLimits:
    """This machine's caps, never raising at a reader. Defaults where there is no file."""
    from agentjobs.dispatch.config import load_dispatch_config

    try:
        config = load_dispatch_config(home)
    except DispatchError:
        config = None
    return config.limits if config is not None else DispatchLimits()


def queue_limit(home: Path) -> int:
    """``limits.dispatch_queue_limit`` for this machine, never raising at a reader."""
    return machine_limits(home).dispatch_queue_limit


def enqueue(
    home: Path,
    project: Project,
    request: DispatchRequest,
    *,
    manager: Optional[TaskManagerLike] = None,
    queued_by: str = "",
    source: str = SOURCE_MANUAL,
    detail: str = "",
    api_base: Optional[str] = None,
) -> QueuedDispatch:
    """Record an authorised dispatch that found no slot. Starts nothing.

    Raises :class:`DispatchQueueFullError` or :class:`AlreadyQueuedError` -- both
    ``DispatchRefused`` subclasses, so the caller that would have rendered the
    concurrency refusal renders these under their own codes without a second branch.

    A ``note`` goes on the task in the same breath, because a queued dispatch is a thing
    that happened to the task and the record is what the next session reads. It is
    written as the dispatcher rather than as the person: the human's *authorising* entry
    is written by ``dispatch_task`` at start time, exactly as it is for a click, and two
    entries that both look like an authorisation would make §2's rule ambiguous.
    """
    store = journal(home)
    try:
        entry = store.enqueue_dispatch(
            project.id,
            request.task_id,
            request={**request_payload(request), "api_base": api_base},
            queued_by=queued_by,
            source=source,
            limit=queue_limit(home),
        )
    except QueueFull as exc:
        raise DispatchQueueFullError(
            f"{exc}. Cancel one of the waiting dispatches, or raise "
            "limits.dispatch_queue_limit in ~/.agentjobs/dispatch.yaml."
        ) from exc
    except AlreadyQueued as exc:
        raise AlreadyQueuedError(str(exc), queue_id=exc.queue_id) from exc

    if manager is not None:
        ahead = max(0, _position(store.queued_dispatches(), entry.queue_id) - 1)
        body = (
            f"A dispatch of this task was queued for the next free slot"
            f"{f' by {queued_by}' if queued_by else ''}. "
            f"{'It is next in line.' if ahead == 0 else f'{ahead} dispatch(es) are ahead of it.'} "
            "Nothing has started: every dispatch gate is judged when a slot frees, and a "
            "gate that refuses then writes here instead."
        )
        if detail:
            body += f"\n\n{detail}"
        _note(manager, request.task_id, body, data={"dispatch_queued": entry.queue_id})
    return entry


def dispatch_or_queue(
    *,
    manager: TaskManagerLike,
    project: Project,
    project_config: Dict[str, object],
    request: DispatchRequest,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    now: Optional[datetime] = None,
    queued_by: str = "",
    source: str = SOURCE_MANUAL,
) -> Union["RunHandle", QueuedDispatch]:
    """``dispatch_task``, and on a full machine the queue when the caller asked for it.

    Two return types, which is honest rather than convenient: the caller has to say which
    happened, and every surface that renders the answer has to say so too.

    The only refusal it intercepts is the concurrency one, and only with
    ``if_full: queue``. Every other gate refuses as it always did -- a dirty tree or a
    closed task is not made queueable by the machine being busy, and enqueueing on one
    would hide a problem behind a wait.

    **Nothing is written before the interception.** ``dispatch_task`` refuses on the
    ceiling before it writes an authorising entry, takes a lock or admits an attempt --
    both of its concurrency refusals do, the second releasing the lock it took -- so an
    enqueued dispatch leaves no half-started run behind it.
    """
    machine_home = _home(home)
    try:
        return dispatch_task(
            manager=manager,
            project=project,
            project_config=project_config,
            request=request,
            home=home,
            api_base=api_base,
            now=now,
        )
    except ConcurrencyLimitError as exc:
        if request.if_full != IF_FULL_QUEUE:
            raise
        return enqueue(
            machine_home,
            project,
            request,
            manager=manager,
            queued_by=queued_by,
            source=source,
            detail=str(exc),
            # The address the *authorising* server answers on, carried so the start can
            # hand the agent the same one a click would have (see `start_api_base`).
            api_base=api_base,
        )


# ----- reading and cancelling ----------------------------------------------------


def waiting(home: Path) -> List[QueuedDispatch]:
    """Every entry still holding a place in line, in FIFO order. Never raises."""
    try:
        return journal(home).queued_dispatches()
    except ExecutionStoreError:
        return []


def find(home: Path, queue_id: str) -> Optional[QueuedDispatch]:
    """One entry by its id, whatever its status. Never raises."""
    try:
        return journal(home).queued_dispatch(queue_id)
    except ExecutionStoreError:
        return None


def cancel(
    home: Path,
    queue_id: str,
    *,
    requester: str = "",
    manager: Optional[TaskManagerLike] = None,
) -> Optional[QueuedDispatch]:
    """Take a waiting dispatch out of the queue. ``None`` if it was not waiting.

    ``None`` covers both "no such entry" and "it has already started", and the caller
    must tell them apart by reading the entry: cancelling a queued dispatch that started
    two seconds ago has to stop the *run*, not report a cancellation that did not happen.
    """
    store = journal(home)
    entry = store.queued_dispatch(queue_id)
    if entry is None or not entry.waiting:
        return None
    if not store.settle_queued_dispatch(
        queue_id,
        status="cancelled",
        detail=f"cancelled by {requester}" if requester else "cancelled",
    ):
        return None
    if manager is not None:
        _note(
            manager,
            entry.task_id,
            f"The queued dispatch of this task was cancelled"
            f"{f' by {requester}' if requester else ''} before it started. Nothing ran.",
            data={"dispatch_queue_cancelled": queue_id},
        )
    return store.queued_dispatch(queue_id)


def position(home: Path, queue_id: str) -> int:
    """Where this entry stands in line, 1-based. ``0`` when it is not waiting."""
    return _position(waiting(home), queue_id)


def _position(entries: Sequence[QueuedDispatch], queue_id: str) -> int:
    for index, entry in enumerate(entries, start=1):
        if entry.queue_id == queue_id:
            return index
    return 0


def start_api_base(entry: QueuedDispatch, fallback: Optional[str]) -> Optional[str]:
    """The address a queued start should tell its agent AgentJobs is at.

    **The one thing a queue entry carries that a request does not: where the server that
    accepted it answers.** A dispatch from the browser derives that from the socket its
    own request arrived on, which is why the reachability gate lets it through; a start
    minutes later happens on a tick that has no request and no socket, so without this it
    falls back to a machine-wide default. On a machine that never wrote ``api_base:`` --
    or wrote it before the dashboard moved port -- the default is dead, and every queued
    dispatch is then refused with ``api_base_unreachable`` at the gate. Observed in
    task-459's own sandbox, where both entries dequeued rather than starting.

    **The stored address is used only while it still answers.** A server restarted on
    another port makes a stored address exactly the stale claim
    ``assert_api_base_answers`` exists to refuse, so the probe is what promotes it from a
    claim to evidence -- and a failed probe hands the question back rather than guessing,
    which means the gate resolves an address and refuses on its own terms.
    """
    stored = entry.request.get("api_base")
    if isinstance(stored, str) and stored.strip():
        if probe_api_base(stored).answered:
            return stored
    return fallback


# ----- starting ------------------------------------------------------------------

#: How the resolver a caller supplies answers: the project and a manager for it, or None.
Resolver = Callable[[str], Optional[Tuple[TaskManagerLike, Project]]]


def free_slots(home: Path) -> int:
    """How many slots this machine has spare, counted the way the guard counts them.

    A cheap pre-check, and nothing rests on it: ``dispatch_task`` counts again inside its
    own transaction and is the authority. What this buys is that a tick on a full machine
    does no work and writes nothing at all, which matters because the tick runs every few
    seconds and the queue is normally full exactly when the machine is.
    """
    ceiling, _ = machine_ceiling(home)
    holding = [run for run in effective_live_runs(home, live_runs(home)) if run.takes_slot]
    return max(0, ceiling - len(holding))


def start_due(
    home: Path,
    *,
    resolve: Resolver,
    api_base: Optional[str] = None,
    now: Optional[Callable[[], datetime]] = None,
    starter: Optional[Callable[..., Any]] = None,
    limit: int = 0,
) -> List[QueueDecision]:
    """Start what the queue owes, if the machine has room. One pass; never raises.

    **Order is arrival, not the task queue's.** A person who queued a specific dispatch
    meant *that* one, and re-sorting their two clicks by the backlog's own band and
    position would answer a question they did not ask -- the backlog order is what the
    pull mode (a sibling task) is for, and it reads ``task_next`` rather than this table.
    Recorded as a decision on task-459 with that alternative named.

    **An entry that cannot start does not starve the ones behind it.** FIFO decides who
    is *offered* the slot first; an entry refused for a transient reason keeps its place
    and the next is tried in the same pass. Strict head-of-line blocking was the
    alternative and is rejected: one task whose own run is still finishing would hold up
    a queue of unrelated work for as long as that run lasted.

    **An entry whose credential is out of quota is not tried at all** (task-463). An open
    usage-limit incident means a run started on that credential would park the moment it
    asked for a turn, so the entry keeps its place and its position and nothing is spent
    on it -- no attempt, no hourly cap, not a word written to its task. The pause is per
    credential, so a queue holding entries for two runners keeps starting the one whose
    subscription still answers. See :mod:`agentjobs.dispatch.start_pause`.

    ``limit`` bounds how many entries one pass may start; ``0`` means "as many as there
    are free slots", which is the application's value.
    """
    clock = now or (lambda: dispatch_clock.utcnow())
    decisions: List[QueueDecision] = []
    try:
        store = journal(home)
        entries = store.queued_dispatches()
    except ExecutionStoreError:
        return decisions
    if not entries:
        return decisions
    incidents = start_pause.open_pauses(home)
    room = free_slots(home)
    budget = min(room, limit) if limit > 0 else room
    if budget <= 0:
        return decisions
    limits = machine_limits(home)
    hourly = check_machine_budget(home, limits, now=clock())
    if hourly is not None:
        # Machine-wide and about to refuse every entry. Said once and acted on for none,
        # because ``dispatch_task``'s own check of this cap *writes the refusal onto the
        # task* -- correct for a click somebody is reading, and a note a minute on twenty
        # tasks if a queue walked into it every tick.
        return [QueueDecision("", "", "", "held", hourly.message)]

    said: set = set()
    for entry in entries:
        if budget <= 0:
            break
        held = _paused(home, entry, incidents)
        if held is not None:
            # Reported once per incident rather than once per entry: a queue of ten
            # entries behind one reset is one fact, and ten copies of it would bury the
            # tick's other lines. Nothing is written to the entry or its task.
            if held.incident_id not in said:
                said.add(held.incident_id)
                decisions.append(QueueDecision("", "", "", "held", held.sentence()))
            continue
        decision = _start_one(
            home,
            store,
            entry,
            resolve=resolve,
            api_base=api_base,
            clock=clock,
            starter=starter,
            limits=limits,
        )
        if decision is None:
            continue
        decisions.append(decision)
        if decision.outcome == "started":
            budget -= 1
    return decisions


def _paused(
    home: Path, entry: QueuedDispatch, incidents: Mapping[str, Any]
) -> Optional["start_pause.Pause"]:
    """The open incident this entry's credential is under, or ``None`` to try it.

    The runner and group are read off the *stored ask*, so an entry that named a runner
    is judged against that runner rather than against whatever the project's ladder would
    pick today -- the same request the start will rebuild.
    """
    if not incidents:
        return None
    stored = entry.request
    return start_pause.pause_for(
        home,
        entry.project_id,
        runner=stored.get("runner"),
        group=stored.get("group"),
        incidents=incidents,
    )


def _start_one(
    home: Path,
    store: ExecutionStore,
    entry: QueuedDispatch,
    *,
    resolve: Resolver,
    api_base: Optional[str],
    clock: Callable[[], datetime],
    starter: Optional[Callable[..., Any]],
    limits: "DispatchLimits",
) -> Optional[QueueDecision]:
    """One queued entry through every gate. Returns what happened, or ``None`` if skipped."""
    from agentjobs.dispatch.guards import AlreadyAdmittedError
    from agentjobs.dispatch.runner import DispatchRunError

    resolved = None
    try:
        resolved = resolve(entry.project_id)
    except Exception:  # noqa: BLE001 - an unresolvable project is reported, not raised
        resolved = None
    if resolved is None:
        return QueueDecision(
            entry.queue_id,
            entry.task_id,
            entry.project_id,
            "unresolvable",
            f"project {entry.project_id} is not registered on this machine right now",
        )
    manager, project = resolved

    # The per-task caps, read before anything is claimed and **without writing**.
    # ``dispatch_task`` checks them again and is the authority; what this avoids is its
    # side effect, which is to record the refusal on the task. That record is the right
    # answer for a click and the wrong one for a queue entry meeting a cooldown at the
    # tick's rate.
    task = manager.get_task(entry.task_id)
    if task is not None:
        cap = check_budget(task, limits.auto, now=clock())
        if cap is not None:
            return QueueDecision(
                entry.queue_id, entry.task_id, entry.project_id, "waiting", cap.message
            )

    claimed = store.claim_queued_dispatch(entry.queue_id, retry_after_seconds=RETRY_AFTER_SECONDS)
    if claimed is None:
        return None  # another tick has it, it is backing off, or it settled meanwhile
    request = rebuild_request(claimed)

    start = starter or dispatch_task
    try:
        handle = start(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=request,
            home=home,
            api_base=start_api_base(claimed, api_base),
            now=clock(),
        )
    except AlreadyAdmittedError as exc:
        # This entry's admission committed and the process that made it died before it
        # could say so. The run exists; recording a second would be the duplicate write
        # the stable operation id exists to prevent.
        store.settle_queued_dispatch(
            entry.queue_id,
            status=QUEUE_STARTED,
            run_id=exc.attempt.run_id,
            detail="already admitted by an earlier attempt to start this entry",
        )
        return QueueDecision(
            entry.queue_id,
            entry.task_id,
            entry.project_id,
            "started",
            "recovered an admission an earlier start had already committed",
            run_id=exc.attempt.run_id,
        )
    except DispatchRunError as exc:
        # Admitted, then failed to launch. dispatch_task concluded the attempt and wrote
        # its own record; the queue entry is spent either way.
        store.settle_queued_dispatch(
            entry.queue_id, status=QUEUE_REFUSED, detail=f"launch failed: {exc}"
        )
        return QueueDecision(
            entry.queue_id, entry.task_id, entry.project_id, "refused", f"launch failed: {exc}"
        )
    except (DispatchRefused, DispatchError) as exc:
        return _refused(store, entry, manager, exc)

    run_id = getattr(handle, "run_id", None)
    store.settle_queued_dispatch(entry.queue_id, status=QUEUE_STARTED, run_id=run_id)
    return QueueDecision(
        entry.queue_id,
        entry.task_id,
        entry.project_id,
        "started",
        f"queued {entry.queued_at}, started when a slot freed",
        run_id=run_id,
    )


def _refused(
    store: ExecutionStore, entry: QueuedDispatch, manager: TaskManagerLike, exc: Exception
) -> QueueDecision:
    """Judge one start-time refusal: wait for it, or take the entry out of the queue."""
    reason = str(getattr(exc, "reason", "dispatch_refused"))
    if reason in TRANSIENT_CLASSES:
        store.release_queued_dispatch(entry.queue_id, detail=f"{reason}: {exc}")
        return QueueDecision(
            entry.queue_id, entry.task_id, entry.project_id, "waiting", f"{reason}: {exc}"
        )
    store.settle_queued_dispatch(entry.queue_id, status=QUEUE_REFUSED, detail=f"{reason}: {exc}")
    _note(
        manager,
        entry.task_id,
        (
            f"The queued dispatch of this task was refused when a slot freed "
            f"(`{reason}`), so it is no longer waiting.\n\n{exc}\n\n"
            "Every dispatch gate is judged at the moment a queued dispatch starts, not "
            "when it was queued. Fix what the refusal names and dispatch again."
        ),
        data={"dispatch_queue_refused": reason, "dispatch_queue_id": entry.queue_id},
    )
    return QueueDecision(
        entry.queue_id, entry.task_id, entry.project_id, "refused", f"{reason}: {exc}"
    )


# ----- plumbing ------------------------------------------------------------------


def _home(home: Optional[Path]) -> Path:
    from agentjobs.dispatch.config import dispatch_config_path

    return dispatch_config_path(home).parent


def _note(
    manager: TaskManagerLike, task_id: str, body: str, *, data: Optional[Dict[str, Any]] = None
) -> None:
    """Write one note on the task, swallowing a storage failure.

    Swallowed because the queue's own transitions are the record that matters and a task
    store that cannot be written is not a reason to leave an entry stuck in ``starting``.
    """
    try:
        manager.add_log_entry(
            task_id,
            actor=DISPATCHER_ACTOR,
            type=LogEntryType.NOTE,
            body=body,
            data=data or {},
        )
    except Exception:  # noqa: BLE001 - see the docstring
        return


__all__ = [
    "QueueDecision",
    "SOURCE_MANUAL",
    "SOURCE_PULL",
    "TRANSIENT_CLASSES",
    "cancel",
    "dispatch_or_queue",
    "enqueue",
    "find",
    "free_slots",
    "machine_limits",
    "position",
    "queue_limit",
    "rebuild_request",
    "request_payload",
    "start_api_base",
    "start_due",
    "waiting",
]
