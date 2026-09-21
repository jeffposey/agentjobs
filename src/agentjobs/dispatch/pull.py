"""The pull mode: a person arms a project with a bound, and free slots fill themselves.

Design section 5 rejected "a polling worker over ``ball == agent``" from the first draft
of this document until 2026-09-18, on two grounds. **Neither is answered by argument
here; both are answered by the shape of this module**, and section 5c states the same
thing in prose:

* *"It turns the ball into an autonomous work queue, which is precisely the unbounded
  loop §2 exists to prevent."* Nothing here reads the ball. It reads
  ``manager.claimable_tasks()`` -- ``task_next``'s answer and its runners-up, in the
  stored queue order -- so what runs is what a person put at the top of the backlog, a
  ``queue move`` is how you steer it, and ``agentjobs next --why`` already answers "why
  that one". And it is not unbounded: an arming carries a bound, the bound is spent
  inside one SQL statement (:meth:`ExecutionStore.spend_pull_start`), and every §7 cap
  binds each start on top of it.

* *"It makes dispatch happen with no log entry to attribute it to, so 'who authorized
  this?' has no answer."* Every start here goes through ``guards.dispatch_task`` with
  ``pull_arming_id`` set, which writes the arming human's authorising entry onto the task
  and then puts *that stored entry* through ``assert_human_clocked`` -- the same check,
  on the same terms, as a click. The identity is read off the arming row, never off a
  request, so there is nothing a tick could forge. This is the epic walk's attribution
  (task-022) applied to a project's queue instead of an epic's children.

**This module starts runs and judges nothing.** Every gate, every cap, the ceiling, the
kill switch, the clean-tree rule and the live-run rule are ``dispatch_task``'s, judged at
the moment of the start and not at the moment of the arming. What is decided here is only
*which task to offer next* and *when to stop offering*.

**It never enqueues.** A pulled dispatch starts into a free slot or does not happen; the
machine's dispatch queue (task-459) is for a dispatch a person asked for, and a pull that
queued would turn a bound on starts into a bound on intentions.

**It is the bottom rung of the slot ladder**, and the two above it are read in
:func:`pull_due`: the dispatch queue, then a live epic walk (task-480). Both are a person
naming work, and this is standing authority to find work; a whole tick is yielded to
either rather than a place in one tick's ordering. The ladder and its argument are one
section of ``docs/agent-dispatch-design.md`` -- *Who gets the next free slot*, in §7.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agentjobs.dispatch import clock as dispatch_clock
from agentjobs.actors import Actor
from agentjobs.dispatch.budget import DISPATCHER_ACTOR
from agentjobs.dispatch.config import (
    DispatchError,
    Posture,
    assert_dispatch_permitted,
)
from agentjobs.dispatch.guards import (
    DispatchRefused,
    DispatchRequest,
    assert_authorizer_is_human,
    dispatch_task,
)
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.queue import free_slots
from agentjobs.dispatch import start_pause
from agentjobs.execution.errors import AlreadyQueued, ExecutionStoreError
from agentjobs.execution.store import (
    BOUND_OPEN,
    BOUND_STARTS,
    BOUND_UNTIL,
    PULL_DISARMED,
    PULL_EXPIRED,
    PULL_FAULTED,
    PULL_SPENT,
    PullArming,
    Supervision,
)
from agentjobs.models_v2 import DispatchTrigger, LogEntryType, Task
from agentjobs.projects import Project
from agentjobs.store_factory import TaskManagerLike

FAILURE_RUN_LIMIT = 3
"""Consecutive failed starts that end an arming, with the reason written on the record.

Three, and the number is about a *machine-wide* fault rather than about three unlucky
tasks. A runner whose command no longer launches, a login that has expired, a disk that
will not take a run directory: each of those refuses every task it is offered, at the
tick's rate, and each refusal costs an hourly-cap slot that the work nobody can start
does not deserve. One failure is ordinary -- a task whose tree is dirty is skipped
without counting here at all, because that is a fact about the task and not about the
machine. Three in a row is the machine.
"""

SKIP_LIMIT = 10
"""How many task-specific refusals one tick will walk past before giving up for now.

The backlog can be long and every refusal costs a read. A tick that walked two hundred
held tasks to find the first startable one would spend a second of the server's time
every ten seconds doing it. Ten is enough to get past a handful of held or dirty tasks
and short enough that the tick stays cheap; the next tick starts again from the top, so
nothing is skipped permanently.
"""


class PullRefused(DispatchRefused):
    """A pulled dispatch could not be authorised. Never reaches a person mid-tick."""

    reason = "pull_refused"


class NotArmedError(PullRefused):
    """The arming this dispatch names is not armed any more."""

    reason = "pull_not_armed"


STOP_THE_PASS = frozenset(
    {
        # About the machine. Nothing about any task would change the answer.
        "concurrency_limit",
        "machine_per_hour",
        "not_configured",
        "disabled",
        "sentinel",
        "invalid_config",
        "api_base_unreachable",
        # About the project. Same argument one scope down: every task in this backlog
        # would be refused for the same reason, so walking ten of them would write ten
        # identical notes and learn nothing.
        "project_not_enabled",
        "dirty_tree",
        "unknown_runner",
        "unknown_group",
        "no_eligible_runner",
        "recorded_runner_unavailable",
        "posture_above_ceiling",
        "unknown_runner_actor",
        "bad_placeholder",
    }
)
"""Refusals that end this project's pass without touching a task record.

Each of these is true of the machine or the repository rather than of the task that
happened to be offered when it was found. The pass stops, the next tick asks again, and
nothing is written -- at the tick's rate a note per refusal would be a note a minute on
whichever task is at the top of somebody's backlog.
"""

QUIET_SKIPS = frozenset({"cooldown", "per_task_per_day", "per_task_lifetime", "live_run_exists"})
"""Task-specific refusals that are walked past **silently**.

They clear on their own -- a cooldown expires, a live run ends, a daily cap rolls over --
and the record already holds the dispatch entry each of them is counting. A note would
tell a later reader something their own log already told them, once per tick.
"""


# ----- the human act a pulled run stands on -----------------------------------


@dataclass(frozen=True)
class PullAuthorization:
    """The arming a pulled dispatch is standing on, and the person who made it.

    The shape of :class:`agentjobs.dispatch.epic.EpicAuthorization`, and for the same
    reason: what a later reader needs from a run they did not start is *who*, *under what
    act*, and *how much of it was left*. The difference is only where the human act is
    recorded -- an epic's is a log entry on the parent task, and this one is a row keyed
    by project, because the pull mode is about a backlog rather than about a task.
    """

    arming: PullArming
    actor: Actor

    def describe(self) -> str:
        """The sentence written onto the pulled task as its authorising entry.

        Names the three things a reader cannot otherwise reconstruct: who armed the mode,
        which arming this run was bought out of, and what bound that arming carries. The
        posture is named when the person chose one, on exactly the epic's terms -- this
        is the only place on *this task's* record where their name and the envelope their
        act bought appear in the same sentence.
        """
        envelope = (
            f" They chose posture `{self.arming.posture}` for the pulled runs, so this "
            "run gets it too."
            if self.arming.posture
            else ""
        )
        return (
            f"{self.actor.display_name} armed the pull mode for "
            f"{self.arming.project_id} ({bound_sentence(self.arming)}), and this run is "
            f"the queue's next task started under that arming ({self.arming.arming_id})."
            f"{envelope} No separate approval of this task was given or is required: "
            "arming the project is what was authorised, and the stored queue order is "
            "what chose this task."
        )

    def data(self) -> Dict[str, object]:
        return {
            "authorizes_dispatch": True,
            "surface": "the pull mode",
            "pull": {
                "arming": self.arming.arming_id,
                "project": self.arming.project_id,
                "armed_by": self.arming.armed_by,
                "armed_at": self.arming.armed_at,
                "bound": bound_sentence(self.arming),
                "start": self.arming.started,
            },
        }


def bound_sentence(arming: PullArming) -> str:
    """What this arming's bound says, in one phrase a person reads on a card."""
    if arming.bound_kind == BOUND_STARTS:
        total = arming.bound_starts or 0
        return f"{arming.started} of {total} starts used"
    if arming.bound_kind == BOUND_UNTIL:
        return f"until {arming.bound_until}"
    return "until disarmed"


def resolve_pull_authorization(
    project_config: Dict[str, object], arming: Optional[PullArming]
) -> PullAuthorization:
    """What a pulled dispatch would be standing on, or a refusal naming what is missing.

    Takes the row rather than an id, because the caller has already read it inside the
    transaction that charged the bound -- and re-reading here would open a window in
    which a disarm lands between the charge and the check.

    The identity is submitted to ``assert_authorizer_is_human`` exactly as the browser's
    claimed one is. It cannot have been forged into the row (arming is human-only and
    server-enforced), and it is checked again anyway: a project whose actor vocabulary
    changed after the arming should stop pulling rather than keep attributing runs to
    somebody it no longer recognises as a person.
    """
    if arming is None or not arming.armed:
        raise NotArmedError(
            "This dispatch names a pull-mode arming that is no longer armed, so there is "
            "no human act for it to stand on. Arm the project again to start pulling."
        )
    actor = assert_authorizer_is_human(project_config, arming.armed_by)
    return PullAuthorization(arming=arming, actor=actor)


# ----- arming and disarming ----------------------------------------------------


class PullArmingError(DispatchError):
    """Arming refused for a reason a person can fix. Raised at the person, not at a tick."""

    reason = "pull_arming_refused"


def arm(
    home: Path,
    project: Project,
    project_config: Dict[str, object],
    *,
    armed_by: str,
    bound_kind: str = BOUND_OPEN,
    bound_starts: Optional[int] = None,
    bound_until: Optional[str] = None,
    posture: Optional[Posture] = None,
) -> PullArming:
    """Switch the pull mode on for one project. Starts nothing; the next tick does.

    Three things are judged **here**, where a person is waiting on the answer, rather
    than at the tick where nobody is:

    * the actor named is a configured human, so the entries every pulled run will carry
      name a real person;
    * the posture asked for is within this project's machine-local ceiling, so an arming
      cannot promise an envelope the dispatch gate will refuse task after task;
    * dispatch is permitted for this project at all, so arming a project whose gates are
      shut fails now instead of looking armed and doing nothing.

    None of the three is trusted later: the tick re-reads the row and ``dispatch_task``
    re-judges everything, because a ceiling can be lowered and a kill switch can be
    dropped between the arming and the start. What this buys is a refusal a person can
    act on instead of a mode that silently never fires.
    """
    assert_authorizer_is_human(project_config, armed_by)
    resolution = assert_dispatch_permitted(project.id, home)
    if posture is not None and not posture.within(resolution.settings.ceiling):
        raise PullArmingError(
            f"The pull mode was asked to run tasks at posture {posture.value!r}, and "
            f"{project.id} is capped at {resolution.settings.ceiling.value!r}. Raise "
            f"`projects.{project.id}.max_posture` in this machine's dispatch.yaml, or "
            "arm at a posture the project allows."
        )
    try:
        return journal(home).arm_pull(
            project.id,
            armed_by=armed_by,
            bound_kind=bound_kind,
            bound_starts=bound_starts,
            bound_until=bound_until,
            posture=posture.value if posture is not None else None,
        )
    except AlreadyQueued as exc:
        raise PullArmingError(str(exc)) from exc
    except ValueError as exc:
        raise PullArmingError(str(exc)) from exc


def disarm(home: Path, project_id: str, *, requester: str = "") -> Optional[PullArming]:
    """Stop the pull mode from starting anything more. **Kills nothing.**

    ``None`` when the project was not armed, which covers a second press of the same
    button and a disarm that raced a bound running out. Both are quiet on purpose: a
    person pressing Disarm wants the mode off, and it is off.

    Runs already going are untouched, deliberately and not as an omission. They were
    authorised individually, each has its own record and its own merge gate, and killing
    work somebody's click bought because a later click withdrew *future* authority would
    destroy the thing the person was trying to protect.
    """
    return journal(home).retire_pull(
        _arming_id(home, project_id) or "",
        state=PULL_DISARMED,
        retired_by=requester,
        detail=f"disarmed by {requester}" if requester else "disarmed",
    )


def armed(home: Path, project_id: str) -> Optional[PullArming]:
    """This project's live arming, or ``None``. Never raises at a reader."""
    try:
        return journal(home).armed_pull(project_id)
    except ExecutionStoreError:
        return None


def armings(home: Path) -> List[PullArming]:
    """Every project armed right now. Never raises at a reader."""
    try:
        return journal(home).armed_pulls()
    except ExecutionStoreError:
        return []


def _arming_id(home: Path, project_id: str) -> Optional[str]:
    current = armed(home, project_id)
    return current.arming_id if current is not None else None


# ----- what it would start next ------------------------------------------------


def next_task(manager: TaskManagerLike) -> Optional[Task]:
    """What ``task_next`` says this project's pull mode would start, or ``None``.

    Never raises, including on a broken queue. This is what the board renders beside
    "armed", and a page that 500s because the backlog needs repairing would hide the
    arming as well as the problem -- while the tick, which reads the same answer through
    :func:`_candidates`, stops and says so where it belongs.
    """
    try:
        return manager.get_next_task()
    except Exception:  # noqa: BLE001 - a reader of a broken queue gets "nothing", not a stack
        return None


def _candidates(manager: TaskManagerLike) -> List[Task]:
    """``task_next``'s answer and its runners-up, in the stored queue order.

    ``get_next_task`` is the head of exactly this list, which is why walking it is still
    "ask ``task_next``" rather than a second ordering: same claimability rules, same
    band, same ``queue_position``, same integrity assertion. What the list buys that the
    head cannot is the ability to *skip*: a task refused for a reason about itself -- a
    dirty tree, a hold, a live run -- has to be walked past inside one tick, or the tick
    spends its whole budget re-offering the same task to the same refusal.
    """
    return list(manager.claimable_tasks())


# ----- the tick ----------------------------------------------------------------


@dataclass(frozen=True)
class PullDecision:
    """What one pass of the pull mode did, for the tick's report."""

    project_id: str
    task_id: str
    outcome: str
    """``started``, ``skipped``, ``held``, ``retired`` or ``idle``."""
    detail: str
    run_id: Optional[str] = None
    arming_id: str = ""

    def describe(self) -> str:
        subject = self.task_id or self.project_id
        run = f" as {self.run_id}" if self.run_id else ""
        return f"{subject} {self.outcome}{run}: {self.detail}"


#: How a caller answers "which project is this, and who manages it".
Resolver = Callable[[str], Optional[Tuple[TaskManagerLike, Project]]]


def walking_now(home: Path) -> List[Supervision]:
    """Every epic walk still flying on this machine, judged at the moment of the pass.

    **Read from the walk's own record, never from anything cached at arming time**
    (task-480). A walk is one row in the execution journal from the click that started it
    until it lands or is grounded, so "is a walk waiting for a slot right now" is a query
    rather than an inference from what a tick happened to see.

    **A ``walking`` row is not by itself a flying walk**, which is the whole of the
    narrowing here. A server-hosted walk is stepped by the same poll that runs the pull
    pass, so while the server is up it is being advanced by construction. An attached one
    is a person's own ``dispatch walk`` process, and that process can die between two
    ticks -- leaving a row nothing will ever close, which ``open_walk`` tolerates because
    its only other reader takes such a walk over. A reader that yielded to it instead
    would idle this machine's slots forever on a supervisor nobody is running, so an
    attached walk counts only while its holder is alive, with the pid-reuse check
    ``open_walk`` already makes against the moment the holder last wrote.
    """
    from agentjobs.dispatch.ledger import process_alive
    from agentjobs.execution.store import process_created_after

    flying: List[Supervision] = []
    for walk in journal(home).open_walks():
        if walk.host == "server":
            flying.append(walk)
            continue
        pid = walk.holder_pid
        if pid is None or not process_alive(int(pid)):
            continue
        moment = _moment(walk.updated_at)
        if moment is not None and process_created_after(int(pid), moment):
            continue  # the number was recycled; the supervisor that wrote this is gone
        flying.append(walk)
    return flying


def walk_sentence(flying: Sequence[Supervision]) -> str:
    """Why a pull pass started nothing, in the terms a person can act on.

    It names the epic rather than the walk id, because the thing a reader of the slot
    board is holding is the epic they clicked.
    """
    first = flying[0].parent_task_id
    more = f" (and {len(flying) - 1} more)" if len(flying) > 1 else ""
    return (
        f"an epic walk on {first}{more} is live, and a person named that epic, "
        "so free slots are its children's"
    )


def pull_due(
    home: Path,
    *,
    resolve: Resolver,
    api_base: Optional[str] = None,
    now: Optional[Callable[[], datetime]] = None,
    starter: Optional[Callable[..., Any]] = None,
) -> List[PullDecision]:
    """Fill free slots from every armed project's queue. One pass; never raises.

    **This is the bottom rung of the slot ladder**, and both rungs above it are read
    here: the machine's dispatch queue, then a live epic walk. The ladder itself, and the
    argument for its order, is one section of ``docs/agent-dispatch-design.md`` --
    *Who gets the next free slot* in section 7. What follows is only what this function
    does about it.

    **The manual dispatch queue goes first, and "first" means it yields the whole tick.**
    Running after ``queue.start_due`` within one tick is not enough: a queued entry
    refused for a transient reason keeps its place and waits, and a pull starting into
    the slot it is waiting for would overtake it at the next tick while looking, from the
    board, like nothing had happened. So while anything is waiting in that queue the pull
    mode starts nothing at all. What that costs is real and worth stating: a manual entry
    parked on a long-lived transient condition -- a task somebody put on hold -- stalls
    the pull mode until it is cancelled. It is visible on the board as a waiting entry,
    which is what makes it a thing a person can fix; a silent overtake would not be.

    **A live epic walk goes second, and yields the whole tick for the same reason**
    (task-480). A walk never enqueues -- it asks ``dispatch_task`` for a slot and treats a
    full machine as backpressure, retried on its next pass -- so before this rule existed
    a waiting walk was invisible here and the pull mode, running on the faster clock, took
    the slots out from under the epic somebody had clicked. Starting after the walk within
    one tick would not fix it: the walk's next pass is seconds away and the tick's is now.
    So while any walk is flying on this machine, free slots are its children's.

    **Round-robin over armed projects, one start each per pass.** A machine runs a
    handful of projects and a pass happens every few seconds, so fairness costs nothing
    and a project with a hundred ready tasks cannot take every slot in one go. Sorting by
    anything else -- how long a project has been armed, how much of its bound is left --
    would be a scheduler, and the backlog's own order is already the decision.
    """
    clock = now or (lambda: dispatch_clock.utcnow())
    decisions: List[PullDecision] = []
    try:
        store = journal(home)
        live = store.armed_pulls()
    except ExecutionStoreError:
        return decisions
    if not live:
        return decisions

    live = [arming for arming in live if not _retire_if_over(store, arming, clock(), decisions)]
    if not live:
        return decisions

    incidents = start_pause.open_pauses(home)
    if incidents:
        going: List[PullArming] = []
        said: set = set()
        for arming in live:
            pause = start_pause.pause_for(home, arming.project_id, incidents=incidents)
            if pause is None:
                going.append(arming)
                continue
            # Held, not retired. The arming keeps its bound, its posture and the identity
            # that authorised it, and starts again on the first tick after task-417's
            # probe closes the incident -- which is the whole of the resume mechanism.
            if pause.incident_id not in said:
                said.add(pause.incident_id)
                decisions.append(PullDecision(arming.project_id, "", "held", pause.sentence()))
        live = going
        if not live:
            return decisions

    from agentjobs.dispatch import queue as dispatch_queue

    if dispatch_queue.waiting(home):
        return [
            PullDecision("", "", "held", "a manual dispatch is waiting for a slot and starts first")
        ]

    try:
        flying = walking_now(home)
    except ExecutionStoreError:
        # The journal answered a moment ago and does not now. Starting a run on a store
        # that cannot be read is the one direction that is not recoverable by waiting.
        return decisions
    if flying:
        return [PullDecision("", "", "held", walk_sentence(flying))]

    room = free_slots(home)
    if room <= 0:
        return decisions

    for arming in live:
        if room <= 0:
            break
        decision = _pull_one(
            home,
            arming,
            resolve=resolve,
            api_base=api_base,
            clock=clock,
            starter=starter,
        )
        decisions.extend(decision)
        if any(item.outcome == "started" for item in decision):
            room -= 1
    return decisions


def _pull_one(
    home: Path,
    arming: PullArming,
    *,
    resolve: Resolver,
    api_base: Optional[str],
    clock: Callable[[], datetime],
    starter: Optional[Callable[..., Any]],
) -> List[PullDecision]:
    """Offer this project's queue to a free slot until one task takes it."""
    from agentjobs.dispatch.runner import DispatchRunError

    store = journal(home)
    decisions: List[PullDecision] = []
    try:
        resolved = resolve(arming.project_id)
    except Exception:  # noqa: BLE001 - an unresolvable project is reported, not raised
        resolved = None
    if resolved is None:
        return [
            PullDecision(
                arming.project_id,
                "",
                "idle",
                f"project {arming.project_id} is not registered on this machine right now",
                arming_id=arming.arming_id,
            )
        ]
    manager, project = resolved

    try:
        candidates = _candidates(manager)
    except Exception as exc:  # noqa: BLE001 - a broken queue stops the mode, loudly
        _retire(
            store,
            arming,
            PULL_FAULTED,
            f"the backlog's order could not be read, so nothing was started: {exc}",
            decisions,
        )
        return decisions
    if not candidates:
        return [
            PullDecision(
                arming.project_id,
                "",
                "idle",
                "the queue has nothing claimable right now",
                arming_id=arming.arming_id,
            )
        ]

    for task in candidates[:SKIP_LIMIT]:
        charged = store.spend_pull_start(arming.arming_id)
        if charged is None:
            # The bound ran out, or somebody disarmed between the read and here. Either
            # way this arming buys nothing more; which of the two it was is what the row
            # says, and `_retire_if_over` on the next pass says it out loud.
            _retire(
                store,
                arming,
                PULL_SPENT,
                f"the bound of {arming.bound_starts} start(s) is spent",
                decisions,
            )
            return decisions
        request = DispatchRequest(
            task_id=task.id,
            trigger=DispatchTrigger.PULL,
            pull_arming_id=arming.arming_id,
        )
        start = starter or dispatch_task
        try:
            handle = start(
                manager=manager,
                project=project,
                project_config=project.load_config(),
                request=request,
                home=home,
                api_base=api_base,
                now=clock(),
            )
        except (DispatchRefused, DispatchError, DispatchRunError) as exc:
            store.refund_pull_start(arming.arming_id)
            verdict = _judge(store, arming, manager, project, task, exc, decisions)
            if verdict == "stop":
                return decisions
            continue

        store.clear_pull_failures(arming.arming_id)
        run_id = getattr(handle, "run_id", None)
        decisions.append(
            PullDecision(
                arming.project_id,
                task.id,
                "started",
                f"pulled by {arming.armed_by}; {bound_sentence(charged)}",
                run_id=run_id,
                arming_id=arming.arming_id,
            )
        )
        if charged.starts_left == 0:
            _retire(
                store,
                arming,
                PULL_SPENT,
                f"the bound of {charged.bound_starts} start(s) is spent",
                decisions,
            )
        return decisions

    decisions.append(
        PullDecision(
            arming.project_id,
            "",
            "idle",
            f"every one of the first {SKIP_LIMIT} tasks in the queue refused a start",
            arming_id=arming.arming_id,
        )
    )
    return decisions


def _judge(
    store: Any,
    arming: PullArming,
    manager: TaskManagerLike,
    project: Project,
    task: Task,
    exc: Exception,
    decisions: List[PullDecision],
) -> str:
    """Decide what one refusal means: skip this task, or stop the pass.

    Four kinds, and the distinction is the whole of what makes the mode tolerable to
    watch:

    * **About the machine or the project** -- a cap, a full machine, the kill switch, a
      dirty working tree, a runner that no longer exists. Nothing about any task would
      change the answer, so the pass stops here and waits for the next tick, writing on
      no task at all. :data:`STOP_THE_PASS` is the list and why.
    * **About the task, and self-clearing** -- a cooldown, its own live run, a per-task
      cap. Walked past silently; :data:`QUIET_SKIPS`.
    * **About the task, and worth saying** -- a hold, an agent's entry being the newest
      one so the task is not human-clocked, a record too thin to brief anybody. Written
      on that task and walked past. It does **not** count toward the failure run: a queue
      with three held tasks at the top is a normal backlog, not a broken machine.
    * **A failed start** -- the launch itself did not work. That is the machine, and it
      is what :data:`FAILURE_RUN_LIMIT` counts.

    **A different classification than the manual queue's**, and deliberately so. There,
    :data:`~agentjobs.dispatch.queue.TRANSIENT_CLASSES` decides whether one entry *keeps
    its place*, because a person asked for that specific task and nothing else will do.
    Here the question is whether to offer a *different* task, and a held task is exactly
    one to walk past rather than to wait on.
    """
    from agentjobs.dispatch.runner import DispatchRunError

    reason = str(getattr(exc, "reason", "dispatch_refused"))
    if not isinstance(exc, DispatchRunError) and reason in STOP_THE_PASS:
        decisions.append(
            PullDecision(
                arming.project_id,
                task.id,
                "held",
                f"{reason}: {exc}",
                arming_id=arming.arming_id,
            )
        )
        return "stop"

    if not isinstance(exc, DispatchRunError) and reason in QUIET_SKIPS:
        decisions.append(
            PullDecision(
                arming.project_id,
                task.id,
                "skipped",
                f"{reason}: {exc}",
                arming_id=arming.arming_id,
            )
        )
        return "skip"

    if isinstance(exc, DispatchRunError):
        run = store.record_pull_failure(arming.arming_id)
        decisions.append(
            PullDecision(
                arming.project_id,
                task.id,
                "skipped",
                f"launch failed ({run} in a row): {exc}",
                arming_id=arming.arming_id,
            )
        )
        if run >= FAILURE_RUN_LIMIT:
            _retire(
                store,
                arming,
                PULL_FAULTED,
                f"{run} starts in a row failed to launch, the last of them on "
                f"{task.id}: {exc}. Something about this machine is wrong rather than "
                "something about the work, so the pull mode stopped instead of spending "
                "the hourly cap finding out. Fix what the error names and arm again.",
                decisions,
            )
            return "stop"
        return "skip"

    _note(
        manager,
        task.id,
        (
            f"The pull mode reached this task and could not start it (`{reason}`), so it "
            f"moved on to the next task in the queue.\n\n{exc}\n\n"
            "Nothing is waiting on this: the mode will offer it again on a later tick if "
            "it is still what the queue says is next. Fix what the refusal names, or move "
            "the task in the queue."
        ),
        data={"pull_skipped": reason, "pull_arming": arming.arming_id},
    )
    decisions.append(
        PullDecision(
            arming.project_id,
            task.id,
            "skipped",
            f"{reason}: {exc}",
            arming_id=arming.arming_id,
        )
    )
    return "skip"


def _retire_if_over(
    store: Any, arming: PullArming, now: datetime, decisions: List[PullDecision]
) -> bool:
    """End an arming whose bound has already run out. True when it did.

    Checked at the top of every pass rather than only after a start, because an ``until``
    bound passes while nothing is happening -- a machine that was full all evening should
    find the arming expired in the morning rather than start one more run at 3am on an
    authority that ended at midnight.
    """
    if arming.bound_kind == BOUND_UNTIL and arming.bound_until:
        end = _moment(arming.bound_until)
        if end is not None and now >= end:
            _retire(
                store,
                arming,
                PULL_EXPIRED,
                f"the arming ran until {arming.bound_until} and that moment has passed",
                decisions,
            )
            return True
    if arming.bound_kind == BOUND_STARTS and arming.starts_left == 0:
        _retire(
            store,
            arming,
            PULL_SPENT,
            f"the bound of {arming.bound_starts} start(s) is spent",
            decisions,
        )
        return True
    return False


def _retire(
    store: Any, arming: PullArming, state: str, detail: str, decisions: List[PullDecision]
) -> None:
    """End an arming and report it once. A second retirement is silently dropped."""
    if store.retire_pull(arming.arming_id, state=state, retired_by=DISPATCHER_ACTOR, detail=detail):
        decisions.append(
            PullDecision(
                arming.project_id, "", "retired", f"{state}: {detail}", arming_id=arming.arming_id
            )
        )


# ``_incident_stall`` used to live here, and disarmed every arming on the machine when
# any incident was open (task-462). task-463 replaced it with a *pause* that keeps the
# arming: see :mod:`agentjobs.dispatch.start_pause` for why holding the authority beats
# throwing it away, and why the judgement is now per credential rather than per machine.


def _moment(raw: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _note(
    manager: TaskManagerLike, task_id: str, body: str, *, data: Optional[Dict[str, Any]] = None
) -> None:
    """Write one note on the task, swallowing a storage failure.

    Swallowed for the reason the dispatch queue's is: the tick's own report is the record
    that matters to whoever is watching, and a task store that will not take a note is
    not a reason to leave an armed project unable to start anything.
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


def posture_of(arming: Optional[PullArming]) -> Optional[Posture]:
    """The posture an arming chose, as a :class:`Posture`, or ``None`` for the default."""
    if arming is None or not arming.posture:
        return None
    try:
        return Posture(arming.posture)
    except ValueError:  # pragma: no cover - the column is written from a Posture
        return None


__all__ = [
    "FAILURE_RUN_LIMIT",
    "NotArmedError",
    "QUIET_SKIPS",
    "STOP_THE_PASS",
    "PullArmingError",
    "PullAuthorization",
    "PullDecision",
    "PullRefused",
    "SKIP_LIMIT",
    "arm",
    "armed",
    "armings",
    "bound_sentence",
    "disarm",
    "next_task",
    "posture_of",
    "pull_due",
    "resolve_pull_authorization",
    "walk_sentence",
    "walking_now",
]
