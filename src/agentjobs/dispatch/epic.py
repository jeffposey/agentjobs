"""Walking an epic's children on the authorisation a human gave the epic (task-022).

Two tasks already own half of this behaviour each. Task-164 decided that whoever holds a
parent task starts a session per child and supervises it. Task-021 decided that a run at
posture ``autonomous`` merges its own work once an objective gate is green. Neither of
them, alone, produces the thing Jeff asked for: *dispatch an epic, walk away, come back
to every child merged*. This module is the composition, and composition is where the risk
lives -- the failure mode is not one bad merge, it is a run that merges a bad child and
then builds four more on top of it.

**The walk is code, not an instruction to an agent, and that is the central decision.**
Every step of the per-child loop the task specified is mechanical: pick the next eligible
child by dependencies (the queue already answers that), start it (dispatch already does
that), watch it to a finish (the four states are enumerated and the signal is the task
record), gate it (the child's own ``agentjobs finish`` ran ``scripts/check.py`` and the
child's record says whether it closed ``completed``), then either continue or stop. An
LLM adds no judgement to any of those, and adds the possibility of getting them wrong at
three in the morning with nobody watching. The one step that *is* judgement --  whether
the parent's own acceptance criteria are met -- is deliberately not here: see
:func:`walk_epic`, which never closes a parent.

The rejected alternative was prose: extend the parent-task protocol in the workflow guide
to describe the loop and let the supervising agent execute it. That is what the guide
described before this module and it is what the demonstration in task-022 was meant to
prove; it was rejected because "unattended" and "an agent remembers to keep polling" are
in tension. The guide already carries the evidence -- *"a supervisor that ends its turn
saying it will check back periodically is not supervising, it is asleep"* -- and a rule
that has already been broken once by the party responsible for keeping it is a rule that
wants a mechanism. Prose still describes the loop, because a supervisor has to know what
the command it runs is doing; it is no longer what performs it.

## Authorisation, which is the part that had to be got right

Design section 2's rule is that a dispatch may only be caused by a stored log entry whose
actor is a human, and the point of it is that agent-starts-agent is *not representable*
rather than capped. A walk that starts five child runs cannot be allowed to weaken that.

It does not. :func:`resolve_epic_authorization` finds the human entry that authorised the
**parent's** dispatch, and each child dispatch writes its own authorising entry naming
that human, that parent and that entry -- then goes through the identical
``assert_human_clocked`` check on the stored row, like every other dispatch. The evidence
is still a row in an append-only log written under a name the project configures as a
person. What changed is only which task the person clicked: they clicked the epic, and
the epic's children were named on its record at the moment they clicked it.

That is a real widening and it is worth stating plainly rather than burying: **one human
act now starts an arbitrarily long chain of runs, and at posture ``autonomous`` an
arbitrarily long chain of merges into ``main``.** What is left holding is the objective
gate each child runs before its own merge, the fact that nothing is ever pushed, and
``main`` being local and therefore recoverable with ``git reset``. The design record says
this in section 6 and it should keep saying it.

## The bound, which is mechanical rather than promised

An unattended loop that retries forever is precisely the runaway design section 7 exists
to prevent, so the retry bound is enforced here rather than asked for in prose:
:data:`CHILD_ATTEMPT_LIMIT` attempts per child per human authorisation -- the first run
and one retry. Attempts are counted off the child's own log, by matching the authorising
entries this module writes against the parent entry they inherited from, so the count
survives the walk dying and being restarted, and a *fresh* human authorisation of the
epic deliberately resets it. That last part is the escape hatch: a human who looks at a
child that burned both attempts and decides it deserves another can dispatch the epic
again, and the record shows they did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from agentjobs.actors import Actor
from agentjobs.manager import TaskManager
from agentjobs.projects import Project, default_home
from agentjobs.models_v2 import (
    Ball,
    DispatchTrigger,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Task,
)

CHILD_ATTEMPT_LIMIT = 2
"""Runs one child may be given per human authorisation of its epic: the first, and one
retry.

Two rather than one because the commonest way a child run ends badly is not a bad child
-- it is a session that died, and the guide already records why that is worth exactly one
more go and no more: *"a child that dies twice is dying for a reason you cannot see from
here, and a supervisor that keeps retrying spends a night proving it."*

Two rather than three because the cost of the bound being too tight is a walk that stops
at three in the morning on something a human would have waved through, and the cost of it
being too loose is a night of tokens spent on a task that was never going to close. Those
are not symmetric.

**A retry is only ever spent on a run that ended without the child finishing.** A child
that closed with a bad outcome, or handed its ball to a person, is not retried at all --
it stopped the walk. See :class:`ChildVerdict`.
"""

EPIC_DATA_KEY = "epic"
"""Key in an authorising entry's ``data`` naming the parent authorisation it inherited.

Descriptive to every reader except this module's attempt counter, which is the one place
it is read back. That is a deliberate exception to the rule
``_write_authorizing_entry`` states -- *nothing reads this back to decide anything* --
and it is safe for a different reason than that rule's: the field this counter reads is
one this module wrote moments earlier in the same act, and the worst a forged one can do
is make a walk stop *sooner* than it had to. There is no value of it that buys an extra
run.
"""

DEFAULT_POLL_SECONDS = 15.0
"""How often the walk re-reads a running child's task record.

The record is a small YAML file on local disk, so the cost is a stat and a parse; the
number is chosen against how quickly a finished child should be noticed, not against
load. The server's own session poller ticks at ten seconds and is what actually settles a
finished run, so anything much below that would just be re-reading a record nothing has
changed yet.
"""

DEFAULT_CHILD_TIMEOUT_SECONDS = 3 * 60 * 60.0
"""How long one child run may go before the walk stops and says so.

A backstop, not a policy. The states in :class:`ChildVerdict` are what normally ends a
child's turn, and every one of them is written to the child's record by something else --
the child itself, or the server's poller reaping a dead session. This exists for the case
where none of that happens, which is a bug somewhere else, and the right response to a
bug somewhere else is to stop and leave a record rather than to wait until morning.
"""


# ----- refusals ---------------------------------------------------------------


class EpicError(Exception):
    """Base for a walk that cannot start. Never raised for a child that went badly."""

    reason = "epic_refused"


class NotAChildError(EpicError):
    reason = "not_a_child"


class ParentNotSupervisedError(EpicError):
    reason = "parent_not_supervised"


class ParentNotHumanClockedError(EpicError):
    reason = "parent_not_human_clocked"


class ChildAttemptsExhaustedError(EpicError):
    reason = "child_attempts_exhausted"


# ----- inheriting the parent's authorisation ----------------------------------


@dataclass(frozen=True)
class EpicAuthorization:
    """The human act a child dispatch is standing on, and what is left of it."""

    parent: Task
    entry: LogEntry
    actor: Actor
    attempts_used: int

    @property
    def attempts_left(self) -> int:
        return max(0, CHILD_ATTEMPT_LIMIT - self.attempts_used)

    def describe(self) -> str:
        """The sentence written onto the child as its authorising entry.

        Names all three things a later reader needs and cannot otherwise reconstruct:
        who, which epic, and which entry on it. The attempt number is there because a
        second entry on the same child is otherwise indistinguishable from a duplicate.
        """
        return (
            f"{self.actor.display_name} authorised a dispatch of {self.parent.id}, and "
            f"this run is attempt {self.attempts_used + 1} of {CHILD_ATTEMPT_LIMIT} at "
            f"one of its children on that authorisation (entry {self.entry.id} on "
            f"{self.parent.id}). No separate approval of this task was given or is "
            "required: the epic is what was authorised."
        )

    def data(self) -> Dict[str, object]:
        return {
            "authorizes_dispatch": True,
            "surface": "the epic walk",
            EPIC_DATA_KEY: {
                "parent": self.parent.id,
                "entry": self.entry.id,
                "attempt": self.attempts_used + 1,
                "limit": CHILD_ATTEMPT_LIMIT,
            },
        }


def parent_authorizing_entry(parent: Task) -> Optional[LogEntry]:
    """The human entry that started this epic, or ``None`` if nothing did.

    Preferred source is the parent's newest ``dispatch`` entry, whose ``caused_by`` names
    the entry the dispatcher itself judged to be a human's. Using the dispatcher's own
    answer rather than re-deriving one means the walk inherits *the same* authorisation
    the parent run is executing under, not a different entry that happens to also be a
    person's.

    The fallback -- the parent's newest entry -- is for a walk run from a shell against an
    epic nobody dispatched. It is the rule ``resolve_causing_entry`` applies to an ordinary
    CLI dispatch, and it reaches the identical human check afterwards, so it widens
    nothing: a human writes a note on the epic, then walks it.

    **With one difference, and it is a necessary one:** ``transition`` entries are skipped.
    An epic has to be `active` before it can be walked, and becoming active writes a
    ``transition`` -- so on the plain reading, claiming an epic in order to supervise it
    destroys the authorisation that caused the claim, every time, and the fallback could
    never fire. Skipping them is narrow rather than convenient: a ``transition`` is
    written *by the manager* as a consequence of a verb, never by anybody as an
    authorisation, so it is not a thing a human could have meant. An agent's own `note`
    or `progress` entry still shadows the human's, and still refuses -- correctly, because
    that means an agent has been working since anyone authorised anything.
    """
    for entry in reversed(parent.log):
        if entry.type is not LogEntryType.DISPATCH:
            continue
        caused_by = entry.data.get("caused_by") if isinstance(entry.data, dict) else None
        if not isinstance(caused_by, int):
            continue
        for candidate in parent.log:
            if candidate.id == caused_by:
                return candidate
        return None
    for entry in reversed(parent.log):
        if entry.type is LogEntryType.TRANSITION:
            continue
        return entry
    return None


def count_attempts(child: Task, *, parent_id: str, entry_id: int) -> int:
    """Runs already started on this child under this exact parent authorisation.

    Counted off the child's own append-only log rather than held in memory, so it is the
    same number whether the walk has been running for an hour or was started thirty
    seconds ago by a second session that knows nothing about the first.
    """
    total = 0
    for entry in child.log:
        if not isinstance(entry.data, dict):
            continue
        marker = entry.data.get(EPIC_DATA_KEY)
        if not isinstance(marker, dict):
            continue
        if marker.get("parent") == parent_id and marker.get("entry") == entry_id:
            total += 1
    return total


def resolve_epic_authorization(
    manager: TaskManager,
    project_config: Dict[str, object],
    child: Task,
) -> EpicAuthorization:
    """What a child dispatch would be standing on, or a refusal naming what is missing.

    Every refusal here is about the *parent*, deliberately. A child is never judged on its
    own authorisation, because it has none and is not supposed to: the whole point is that
    the human clicked the epic.
    """
    from agentjobs.dispatch.guards import assert_human_clocked

    if not child.parent:
        raise NotAChildError(
            f"{child.id} has no parent, so there is no epic authorisation for it to "
            "inherit. Dispatch it on its own human-authored entry, the ordinary way."
        )

    parent = manager.get_task(child.parent)
    if parent is None:
        raise ParentNotSupervisedError(
            f"{child.id} names {child.parent!r} as its parent and no such task exists, so "
            "nothing authorises a run on it. Fix the parent field or dispatch it "
            "directly."
        )
    if parent.lifecycle is not Lifecycle.ACTIVE:
        raise ParentNotSupervisedError(
            f"{parent.id} is {parent.lifecycle.value}, not active. A child inherits the "
            "authorisation of an epic somebody is currently working; an epic nobody has "
            "claimed has not been started, and one already closed is finished. Claim or "
            "dispatch the parent first."
        )

    entry = parent_authorizing_entry(parent)
    if entry is None:
        raise ParentNotHumanClockedError(
            f"{parent.id} has no log entry a dispatch could be caused by, so it cannot "
            "authorise one for its children either. Every dispatch traces to a human act "
            "(design section 2)."
        )
    try:
        actor = assert_human_clocked(project_config, entry)
    except Exception as exc:  # noqa: BLE001 - re-raised as this module's refusal
        raise ParentNotHumanClockedError(
            f"{parent.id}'s authorising entry ({entry.id}, by {entry.actor!r}) is not a "
            f"human's, so it cannot authorise runs on its children: {exc}"
        ) from exc

    used = count_attempts(child, parent_id=parent.id, entry_id=entry.id)
    return EpicAuthorization(parent=parent, entry=entry, actor=actor, attempts_used=used)


def assert_attempts_remain(authorization: EpicAuthorization, child: Task) -> None:
    """Refuse a run this authorisation has no attempt left to pay for."""
    if authorization.attempts_left > 0:
        return
    raise ChildAttemptsExhaustedError(
        f"{child.id} has already been run {authorization.attempts_used} time(s) on "
        f"{authorization.parent.id}'s authorisation (entry {authorization.entry.id}), "
        f"which is the limit of {CHILD_ATTEMPT_LIMIT}. The walk stops here rather than "
        "spending the night on it. A human who wants it tried again can authorise "
        f"{authorization.parent.id} afresh, which starts a new budget and leaves a "
        "record that they did."
    )


# ----- what one child's turn came to ------------------------------------------


class ChildVerdict(Enum):
    """How a child's turn ended, and therefore whether the walk continues.

    The four states are the ones the workflow guide already enumerates for a supervising
    agent, with the same signal -- the task record, never the process. ``DIED`` is the
    only one a retry is spent on, and that asymmetry is the point: a session that
    vanished says nothing about the work, while a child that closed badly or asked for a
    person has said something and been ignored if the walk carries on.
    """

    COMPLETED = "completed"
    """Closed with outcome ``completed``. Its own gate ran and its own merge happened."""

    CLOSED_UNRESOLVED = "closed_unresolved"
    """Closed with any other outcome. Deliberate, and not a thing to walk past."""

    PARKED = "parked"
    """Ball with a human or an external party. The one state that needs somebody."""

    DIED = "died"
    """The run ended, the child is still open, and nothing new was written."""

    TIMED_OUT = "timed_out"
    """Nothing terminal happened inside the ceiling. A bug elsewhere; stop and say so."""

    @property
    def is_clean(self) -> bool:
        return self is ChildVerdict.COMPLETED

    @property
    def is_retryable(self) -> bool:
        return self is ChildVerdict.DIED


@dataclass(frozen=True)
class ChildAttempt:
    """One dispatch of one child and what became of it."""

    child_id: str
    attempt: int
    run_id: Optional[str]
    verdict: ChildVerdict
    detail: str

    def describe(self) -> str:
        run = self.run_id or "no run"
        return (
            f"{self.child_id} attempt {self.attempt} ({run}): {self.verdict.value} -- {self.detail}"
        )


class WalkStop(Enum):
    """Why a walk ended. Exactly one of these is true of every finished walk."""

    ALL_CHILDREN_DONE = "all_children_done"
    """No open child remains. The only ending that is not a stop for cause."""

    CHILD_NEEDS_A_HUMAN = "child_needs_a_human"
    CHILD_CLOSED_UNRESOLVED = "child_closed_unresolved"
    CHILD_EXHAUSTED_ATTEMPTS = "child_exhausted_attempts"
    CHILD_TIMED_OUT = "child_timed_out"
    COULD_NOT_START_CHILD = "could_not_start_child"
    NO_ELIGIBLE_CHILD = "no_eligible_child"
    """Open children remain and none of them is claimable -- every one is blocked,
    claimed elsewhere, or holding open children of its own. Not the same as being done,
    and reported differently so nobody reads a deadlocked graph as a finished epic."""

    @property
    def is_success(self) -> bool:
        return self is WalkStop.ALL_CHILDREN_DONE


@dataclass
class WalkResult:
    """Everything the walk did, in the order it did it."""

    parent_id: str
    stop: WalkStop
    attempts: List[ChildAttempt] = field(default_factory=list)
    detail: str = ""

    @property
    def merged_children(self) -> List[str]:
        return [a.child_id for a in self.attempts if a.verdict.is_clean]

    def summary(self) -> str:
        lines = [f"Walk of {self.parent_id}: {self.stop.value}."]
        if self.detail:
            lines.append(self.detail)
        if self.attempts:
            lines.append("")
            lines.append("Children, in the order the walk took them:")
            lines.extend(f"  {a.describe()}" for a in self.attempts)
        else:
            lines.append("No child was started.")
        return "\n".join(lines)


# ----- the walk ---------------------------------------------------------------


@dataclass
class WalkSettings:
    """The knobs, all of them bounds rather than behaviour."""

    poll_seconds: float = DEFAULT_POLL_SECONDS
    child_timeout_seconds: float = DEFAULT_CHILD_TIMEOUT_SECONDS
    max_children: Optional[int] = None
    """Hard ceiling on children started in one walk. ``None`` means the epic's own count,
    which is already finite; the option exists so a first run against a wide epic can be
    told to stop after two."""


def next_eligible_child(manager: TaskManager, parent_id: str) -> Optional[Task]:
    """The child the queue says is next, or ``None`` if none is claimable.

    Delegates to the queue rather than sorting children here. The order of work is a
    decision the queue owns and records, and a second implementation of it inside the
    walk is exactly how the dashboard and the walk would come to disagree about what is
    next -- with the walk winning silently, because it is the one that spends money.
    """
    return manager.get_next_task(parent=parent_id)


def open_children(manager: TaskManager, parent_id: str) -> List[Task]:
    children = manager.get_subtasks(parent_id)
    return [child for child in children if child.is_open]


def walk_epic(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    parent_id: str,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    settings: Optional[WalkSettings] = None,
    dispatch: Optional[Callable[..., object]] = None,
    read_run_status: Optional[Callable[[str], Optional[str]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    on_event: Optional[Callable[[str], None]] = None,
) -> WalkResult:
    """Start each eligible child in turn, watch it, and stop the moment one is not clean.

    **It never closes the parent, and that is not an omission.** The task's own
    constraint is that a parent closes only when its acceptance criteria are supported by
    the children's evidence, and whether they are is the one judgement in this whole loop
    that is not mechanical. So a walk that gets to the end writes what every child did
    onto the parent's record and hands back; whoever called it -- the supervising session,
    or a person at a shell -- reads its own criteria against that and closes the parent,
    or does not.

    **One bad child stops everything, rather than being skipped.** A sibling that depended
    on the failed child would now be building on a gap, and the whole premise of this
    feature is that there is nobody awake to notice. The cost is a walk that halts on a
    child a person would have waved through; that is the cheaper of the two mistakes, and
    it is the one that leaves a record.

    Injection points exist for the tests and for nothing else: ``dispatch``,
    ``read_run_status``, ``sleep`` and ``now``. A walk is a loop over processes that cost
    money and take an hour, and the alternative to injecting them is a test suite that
    proves the stop rule by not testing it.
    """
    from agentjobs.dispatch.guards import DispatchRefused, DispatchRequest, dispatch_task
    from agentjobs.dispatch.ledger import find_run

    settings = settings or WalkSettings()
    announce = on_event or (lambda _message: None)
    result = WalkResult(parent_id=parent_id, stop=WalkStop.ALL_CHILDREN_DONE)

    # Defaulted rather than left None. A walk that cannot read run status is not
    # slightly worse -- it loses the *only* signal that tells a dead child from a
    # thinking one, and degrades to waiting out the per-child ceiling on every death.
    # That was observed as a twelve-minute stall on the first real walk, and it looked
    # exactly like a child working.
    ledger_home = Path(home) if home is not None else default_home()

    def status_of(run_id: str) -> Optional[str]:
        if read_run_status is not None:
            return read_run_status(run_id)
        try:
            return find_run(ledger_home, run_id).status
        except Exception:  # noqa: BLE001 - an unreadable run is "cannot tell", not fatal
            return None

    def start_child(child: Task) -> object:
        request = DispatchRequest(
            task_id=child.id,
            trigger=DispatchTrigger.CHILD,
            on_behalf_of_parent=True,
        )
        starter = dispatch or dispatch_task
        return starter(
            manager=manager,
            project=project,
            project_config=project_config,
            request=request,
            home=home,
            api_base=api_base,
        )

    started = 0
    retry_of: Optional[str] = None
    while True:
        # Every fact this loop turns on is written by another process, and the CLI holds
        # one corpus snapshot for a whole invocation. Dropping it here is what makes the
        # walk's reads reads.
        manager.storage.refresh()
        remaining = open_children(manager, parent_id)
        if not remaining:
            result.stop = WalkStop.ALL_CHILDREN_DONE
            result.detail = (
                f"No open child of {parent_id} remains. The parent is deliberately left "
                "open: whether its own acceptance criteria are met is a judgement this "
                "walk does not make."
            )
            return result

        if settings.max_children is not None and started >= settings.max_children:
            result.stop = WalkStop.NO_ELIGIBLE_CHILD
            result.detail = (
                f"Stopped after {started} child/children because --max-children said to. "
                f"{len(remaining)} open child/children remain: "
                f"{', '.join(child.id for child in remaining)}."
            )
            return result

        # A retry goes back to the same child rather than back to the queue. It has to:
        # the first attempt claimed it, so it is `active` and the queue -- correctly --
        # will not offer a claimed task to anybody. Asking the queue again would report
        # the epic as deadlocked on the very child the walk is about to try again.
        child = manager.get_task(retry_of) if retry_of else next_eligible_child(manager, parent_id)
        retry_of = None
        if child is None:
            result.stop = WalkStop.NO_ELIGIBLE_CHILD
            result.detail = (
                f"{len(remaining)} open child/children remain and none is claimable: "
                f"{', '.join(child.id for child in remaining)}. Each is blocked by an "
                "unmet dependency, already claimed, or holding open children of its own. "
                "This is not a finished epic and is reported separately from one."
            )
            return result

        authorization = resolve_epic_authorization(manager, project_config, child)
        try:
            assert_attempts_remain(authorization, child)
        except ChildAttemptsExhaustedError as exc:
            result.stop = WalkStop.CHILD_EXHAUSTED_ATTEMPTS
            result.detail = str(exc)
            return result

        attempt_number = authorization.attempts_used + 1
        announce(f"Starting {child.id} (attempt {attempt_number} of {CHILD_ATTEMPT_LIMIT}).")
        try:
            handle = start_child(child)
        except DispatchRefused as exc:
            result.stop = WalkStop.COULD_NOT_START_CHILD
            result.detail = (
                f"{child.id} could not be started ({getattr(exc, 'reason', 'refused')}): " f"{exc}"
            )
            result.attempts.append(
                ChildAttempt(
                    child_id=child.id,
                    attempt=attempt_number,
                    run_id=None,
                    verdict=ChildVerdict.DIED,
                    detail=str(exc),
                )
            )
            return result

        started += 1
        run_id = getattr(handle, "run_id", None)
        attempt = _watch_child(
            manager=manager,
            child_id=child.id,
            attempt_number=attempt_number,
            run_id=run_id,
            settings=settings,
            status_of=status_of,
            sleep=sleep,
            now=now,
            announce=announce,
        )
        result.attempts.append(attempt)
        announce(attempt.describe())

        if attempt.verdict.is_clean:
            continue
        if attempt.verdict.is_retryable:
            # The next pass re-reads the attempt count off the record, so the bound is
            # enforced by the same code whether the retry happens here or in a walk
            # somebody starts tomorrow. Nothing is counted in memory.
            retry_of = child.id
            continue

        result.stop = {
            ChildVerdict.PARKED: WalkStop.CHILD_NEEDS_A_HUMAN,
            ChildVerdict.CLOSED_UNRESOLVED: WalkStop.CHILD_CLOSED_UNRESOLVED,
            ChildVerdict.TIMED_OUT: WalkStop.CHILD_TIMED_OUT,
        }[attempt.verdict]
        result.detail = attempt.detail
        return result


def _watch_child(
    *,
    manager: TaskManager,
    child_id: str,
    attempt_number: int,
    run_id: Optional[str],
    settings: WalkSettings,
    status_of: Callable[[str], Optional[str]],
    sleep: Callable[[float], None],
    now: Callable[[], float],
    announce: Callable[[str], None],
) -> ChildAttempt:
    """Watch one child to a terminal state, reading the record and not the process.

    ``ball`` is the signal, exactly as the workflow guide says: a child parked on review
    has a live process and is the one state that needs somebody, so a process-liveness
    check would report it as healthy for as long as anybody left it there. The run status
    is consulted for one question only -- *is the session still there* -- and only to
    tell a child that died apart from a child that is still thinking.
    """
    from agentjobs.dispatch.guards import TERMINAL_RUN_STATUSES

    deadline = now() + settings.child_timeout_seconds
    while True:
        # **Liveness first, then the record, and the order is the whole correctness
        # argument.** A child writes its last word -- closed, or handed off -- and then
        # its process exits; only after that does anything mark the run terminal. So a
        # record read *after* a terminal status is guaranteed to include that last word,
        # and a record read before it is not.
        #
        # Reading them the other way round is a race with a window of milliseconds and it
        # fires. On the first full walk of the sandbox epic, all three children merged
        # cleanly and all three were reported dead and re-run: the walk read each record
        # a moment before the finish closed it, then read a run status that had gone
        # terminal in between, and concluded the session had gone without finishing. The
        # damage is not cosmetic -- it spends the child's retry, and the retry re-does
        # work that is already on `main`.
        status = status_of(run_id) if run_id else None
        manager.storage.refresh()
        child = manager.get_task(child_id)
        if child is None:
            return ChildAttempt(
                child_id=child_id,
                attempt=attempt_number,
                run_id=run_id,
                verdict=ChildVerdict.DIED,
                detail="The task record disappeared while the walk was watching it.",
            )

        if child.lifecycle is Lifecycle.CLOSED:
            outcome = child.outcome
            if outcome is Outcome.COMPLETED:
                return ChildAttempt(
                    child_id=child_id,
                    attempt=attempt_number,
                    run_id=run_id,
                    verdict=ChildVerdict.COMPLETED,
                    detail="closed completed; its own gate ran and its own merge happened",
                )
            return ChildAttempt(
                child_id=child_id,
                attempt=attempt_number,
                run_id=run_id,
                verdict=ChildVerdict.CLOSED_UNRESOLVED,
                detail=(
                    f"closed with outcome {outcome.value if outcome else 'none'}, which "
                    "is a deliberate act by whoever closed it and not something to walk "
                    "past"
                ),
            )

        if child.ball is not Ball.AGENT:
            reason = child.ball_reason.value if child.ball_reason else "unstated"
            holder = child.ball.value if child.ball else "nobody"
            return ChildAttempt(
                child_id=child_id,
                attempt=attempt_number,
                run_id=run_id,
                verdict=ChildVerdict.PARKED,
                detail=(
                    f"ball is {holder}/{reason}: " f"{child.ball_prompt or 'no prompt recorded'}"
                ),
            )

        if status is not None and status in TERMINAL_RUN_STATUSES:
            return ChildAttempt(
                child_id=child_id,
                attempt=attempt_number,
                run_id=run_id,
                verdict=ChildVerdict.DIED,
                detail=(
                    f"run ended {status!r} with the child still open and its ball still "
                    "with the agent, so the session went without finishing"
                ),
            )

        if now() >= deadline:
            return ChildAttempt(
                child_id=child_id,
                attempt=attempt_number,
                run_id=run_id,
                verdict=ChildVerdict.TIMED_OUT,
                detail=(
                    f"nothing terminal happened in "
                    f"{settings.child_timeout_seconds / 3600:.1f}h. Something upstream "
                    "is not settling this run; the walk stops rather than waiting for "
                    "morning"
                ),
            )

        sleep(settings.poll_seconds)


# ----- what the walk writes onto the parent -----------------------------------


def walk_report(result: WalkResult, *, started_at: Optional[datetime] = None) -> str:
    """The body of the ``progress`` entry a walk appends to the parent.

    Written whichever way the walk ended, and written to the *parent*, because the parent
    is the only record that has a view of the whole set. Each child's own record already
    says what that child did; what nothing else says is the order they were taken in and
    where the walk stopped.
    """
    when = (started_at or datetime.now(timezone.utc)).isoformat()
    header = "**Epic walk**"
    if result.stop.is_success:
        header += " -- every open child is done."
    else:
        header += f" -- stopped: {result.stop.value}."
    body = [header, "", f"Started {when}.", "", result.summary()]
    if not result.stop.is_success:
        body += [
            "",
            "**The walk stops on the first child that is not clean rather than skipping "
            "it.** A sibling that depended on it would be building on a gap, and nobody "
            "is awake to notice. Whatever the child's record says it needs is what this "
            "epic needs next.",
        ]
    else:
        body += [
            "",
            "**This parent is deliberately still open.** No open child remains, which is "
            "not the same as the parent's acceptance criteria being met. Evaluate them "
            "against the children's evidence and close it only where that evidence "
            "supports it.",
        ]
    return "\n".join(body)


def walk_handoff_prompt(result: WalkResult) -> str:
    """What the parent's ball prompt becomes when a walk stops for cause."""
    failing = result.attempts[-1] if result.attempts else None
    who = failing.child_id if failing else "no child"
    return (
        f"The epic walk stopped on {who}: {result.detail} "
        f"{len(result.merged_children)} child/children completed before it "
        f"({', '.join(result.merged_children) or 'none'}). Read that child's record, "
        "decide what it needs, and restart the walk when it is resolved -- it re-reads "
        "the attempt count off each child's log, so nothing is double-spent."
    )


def describe_settings(settings: WalkSettings) -> Sequence[str]:
    """The bounds, printed before a walk starts so nobody has to guess at them."""
    return (
        f"attempts per child: {CHILD_ATTEMPT_LIMIT} (first run plus one retry)",
        f"poll interval: {settings.poll_seconds:.0f}s",
        f"per-child ceiling: {settings.child_timeout_seconds / 3600:.1f}h",
        f"children this walk may start: {settings.max_children or 'every open one'}",
    )
