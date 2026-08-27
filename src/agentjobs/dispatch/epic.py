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

## The envelope the children run at (task-316)

The widening above is stated in terms of posture -- *"at posture ``autonomous`` an
arbitrarily long chain of merges"* -- and for the first three months of this module's
life that sentence described behaviour the code did not have. ``start_child`` built a
``DispatchRequest`` with no posture on it, so every child fell through to the project
default however the parent had been dispatched. An epic authorised ``autonomous`` on a
project defaulting to ``auto`` therefore ran its first child at ``auto``, that child
correctly handed off for review, and the walk stopped with ``CHILD_NEEDS_A_HUMAN`` --
which reads as a child that needs a decision rather than one handed the wrong authority.
Meanwhile the supervisor's own generated prompt told it the opposite. Task-269's epic is
the incident.

A child now inherits the parent run's posture, and :data:`INHERITABLE_POSTURE_SOURCES`
records which of the four sources may cross that boundary and why the others may not.
The rule is the same one the authorisation runs on: what crosses is a human's act.

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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from agentjobs.actors import Actor
from agentjobs.dispatch.config import Posture, PostureSource
from agentjobs.manager import TaskManager
from agentjobs.queue import order_key
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
    posture: Optional[Posture] = None
    """The envelope that human chose for the epic, when they chose one (task-316).

    ``None`` whenever the parent's own run took its posture from the project default or
    from the parent's task record -- see :func:`inherited_posture` for why only one of
    the sources crosses this boundary. ``None`` is not "unknown": it means nothing about
    this epic overrides what the child would have got anyway.
    """

    @property
    def attempts_left(self) -> int:
        return max(0, CHILD_ATTEMPT_LIMIT - self.attempts_used)

    def describe(self) -> str:
        """The sentence written onto the child as its authorising entry.

        Names all three things a later reader needs and cannot otherwise reconstruct:
        who, which epic, and which entry on it. The attempt number is there because a
        second entry on the same child is otherwise indistinguishable from a duplicate.

        The posture is named when one is inherited, because it is the only place on the
        *child's* record where the human's name and the envelope their click bought
        appear in the same sentence. Everything else about it -- source, ceiling, what
        was clamped -- is on the child's `dispatch` entry moments later; this is the
        attribution.
        """
        envelope = (
            f" They chose posture `{self.posture.value}` for the epic, so this run gets " "it too."
            if self.posture is not None
            else ""
        )
        return (
            f"{self.actor.display_name} authorised a dispatch of {self.parent.id}, and "
            f"this run is attempt {self.attempts_used + 1} of {CHILD_ATTEMPT_LIMIT} at "
            f"one of its children on that authorisation (entry {self.entry.id} on "
            f"{self.parent.id}).{envelope} No separate approval of this task was given "
            "or is required: the epic is what was authorised."
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


INHERITABLE_POSTURE_SOURCES = frozenset({PostureSource.DISPATCH, PostureSource.EPIC})
"""Which sources of a parent run's posture cross into its children (task-316).

**Only a posture a person chose at the moment they authorised the epic**, and the
``EPIC`` entry is that same choice one generation further down -- an epic whose child is
itself an epic passes the original click on rather than dropping it at the second level.

The two that are deliberately absent are the whole decision:

* ``TASK`` -- the parent record's own ``posture:`` field -- does not propagate. It is a
  git-tracked value any agent that can write the repository can set, and letting it
  cross would mean one agent editing one field on its own parent widens *every* child in
  the epic at once. ``resolve_posture`` clamps that source to the project ceiling, which
  bounds the damage on a narrow project and bounds nothing at all on a project whose
  ceiling is already ``autonomous``. A posture on a task record is a statement about
  that task's run; treating it as a statement about a dozen other tasks' runs is a
  widening nobody asked for.
* ``PROJECT`` -- the default in ``dispatch.yaml`` -- does not need to. It already reaches
  every child on its own, as the bottom of ``resolve_posture``'s precedence, and
  "inheriting" it would only relabel a run's ``posture_source`` as ``epic`` while
  changing nothing about what the run may do. That relabelling is worse than useless: it
  would point a reader at the parent for an answer that is in ``dispatch.yaml``.

The principle is the one the walk's *authorisation* already runs on: what crosses the
parent/child boundary is a human's act, and only that.
"""


def parent_dispatch_entry(parent: Task) -> Optional[LogEntry]:
    """The parent's newest ``dispatch`` entry, which is the run supervising this walk.

    Only the manager may append this type (``MANAGER_WRITTEN_LOG_TYPES``), so unlike an
    ordinary note it is an assertion nothing reachable over the API can forge. That is
    what makes it safe to read a posture back out of.
    """
    for entry in reversed(parent.log):
        if entry.type is LogEntryType.DISPATCH:
            return entry
    return None


def inherited_posture(parent: Task) -> Optional[Posture]:
    """The envelope a child should be started at, or ``None`` to decide it locally.

    Read off the parent's newest ``dispatch`` entry -- the same entry
    :func:`parent_authorizing_entry` takes the authorisation from, so a child inherits
    the posture of *the run that is walking it* rather than of some earlier run of the
    same epic.

    ``None`` for every source outside :data:`INHERITABLE_POSTURE_SOURCES`, and for an
    entry written before task-308, where ``posture_source`` is absent and the field's
    own documentation says an absent value reads as ``project`` -- never as unknown. An
    unparseable posture is also ``None``: this decides what a run may do, so a value
    nobody can read is a reason to fall back to the project's default rather than to
    guess at what was meant.
    """
    entry = parent_dispatch_entry(parent)
    if entry is None or not isinstance(entry.data, dict):
        return None
    try:
        source = PostureSource(entry.data.get("posture_source"))
    except ValueError:
        return None
    if source not in INHERITABLE_POSTURE_SOURCES:
        return None
    try:
        return Posture(entry.data.get("posture"))
    except ValueError:
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
    return EpicAuthorization(
        parent=parent,
        entry=entry,
        actor=actor,
        attempts_used=used,
        posture=inherited_posture(parent),
    )


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
    """Everything the walk did, in the order the children *landed*."""

    parent_id: str
    stop: WalkStop
    attempts: List[ChildAttempt] = field(default_factory=list)
    detail: str = ""
    peak_in_flight: int = 0
    """The most children this walk had running at one moment.

    Recorded because it is the evidence for the whole of task-223 and because it is the
    one number a reader cannot recover from the attempt list: attempts are ordered by
    when each child finished, which says nothing about what was running beside it. A
    walk that reports 1 here either had a serial graph or never got a second slot, and
    those are worth telling apart.
    """

    @property
    def merged_children(self) -> List[str]:
        return [a.child_id for a in self.attempts if a.verdict.is_clean]

    def summary(self) -> str:
        lines = [f"Walk of {self.parent_id}: {self.stop.value}."]
        if self.detail:
            lines.append(self.detail)
        if self.attempts:
            lines.append("")
            concurrency = (
                f" (up to {self.peak_in_flight} in flight at once)" if self.peak_in_flight else ""
            )
            lines.append(f"Children, in the order they landed{concurrency}:")
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

    max_concurrent: int = 1
    """Children this walk may have **in flight** at once (task-223).

    **There is only one concurrency cap on this machine and it is not this one.** Since
    task-022 a child is an ordinary dispatch, so it is counted against
    ``limits.max_concurrent_runs`` in ``~/.agentjobs/dispatch.yaml`` by ``dispatch_task``
    like anything else -- the sentence in task-081's brief saying children are
    uncounted subprocesses stopped being true then. This number can only ever narrow
    that ceiling, and ``agentjobs dispatch walk`` fills it in from the machine's limit
    so that the default behaviour is the machine's own answer rather than a second
    number to keep in step.

    The dataclass default is 1 because a ``WalkSettings()`` constructed with no argument
    is a caller who has not thought about it, and the safe reading of that is the
    behaviour that existed before this field.

    Hitting the machine ceiling mid-walk is **backpressure, not a refusal**: the walk
    stops trying for this poll and tries again, because something else on the machine
    holding a slot is a normal condition and not a fact about this epic.
    """


def next_eligible_child(manager: TaskManager, parent_id: str) -> Optional[Task]:
    """The child the queue says is next, or ``None`` if none is claimable."""
    return manager.get_next_task(parent=parent_id)


def frontier(manager: TaskManager, parent_id: str, *, exclude: Sequence[str] = ()) -> List[Task]:
    """Every child that could start **right now**, in the order to start them in.

    This is the rolling frontier, and the word doing the work is *now*. It is recomputed
    from scratch on every pass rather than maintained as a ready-queue that completing
    children push onto, and that is a correctness choice rather than a stylistic one:
    the question a scheduler must ask is *"are all of this child's needs satisfied?"*,
    never *"did the child that just finished name me?"*. A diamond -- a child with two
    unmet needs, one of which just closed -- is the case where those two differ, and the
    push-on-completion version starts it against a prerequisite that has not landed.
    Asking the claimability filter afresh cannot get that wrong, because it is the same
    filter the queue, the CLI and the dashboard ask.

    **The graph decides eligibility, the queue decides order, and out-degree breaks the
    ties the queue does not.** ``claimable_tasks`` has already applied the first two.
    Out-degree -- how many open tasks still ``need`` this one -- is the classic
    critical-path heuristic, and starting a leaf ahead of a child that gates three others
    idles three slots later for no gain. It is deliberately last: the queue holds a
    human's explicit decision about order, and a scheduler that sorted the frontier by
    out-degree outright would quietly overrule every ``queue move`` anybody made. In
    practice ``(band, queue_position)`` is already a total order over open work, so this
    tie-break rarely fires at all -- which is the intended outcome, not a defect in it.

    ``exclude`` is the children already in flight. They are ``active`` and so are
    filtered out by claimability anyway; naming them closes the window between starting
    one and its claim being visible on disk.
    """
    excluded = set(exclude)
    candidates = [
        task for task in manager.claimable_tasks(parent=parent_id) if task.id not in excluded
    ]
    if len(candidates) < 2:
        return candidates
    facts = manager.dependency_facts()

    def key(task: Task) -> Tuple[Tuple[int, int], int]:
        fact = facts.get(task.id)
        return (order_key(task), -(fact.unblocks_count if fact else 0))

    candidates.sort(key=key)
    return candidates


def open_children(manager: TaskManager, parent_id: str) -> List[Task]:
    children = manager.get_subtasks(parent_id)
    return [child for child in children if child.is_open]


@dataclass
class Flight:
    """One child currently in the air: what was started, and when to give up on it."""

    child_id: str
    attempt: int
    run_id: Optional[str]
    deadline: float


def walk_epic(
    *,
    manager: TaskManager,
    project: Project,
    project_config: Dict[str, object],
    parent_id: str,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    settings: Optional[WalkSettings] = None,
    posture: Optional[Posture] = None,
    dispatch: Optional[Callable[..., object]] = None,
    read_run_status: Optional[Callable[[str], Optional[str]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    on_event: Optional[Callable[[str], None]] = None,
) -> WalkResult:
    """Keep every eligible child flying, watch them all, and stop taking off on a bad one.

    **A rolling frontier, not waves** (task-223). At every moment, every child whose
    ``needs`` are all satisfied and which is not already running is started, up to the
    slot count. A child completing recomputes eligibility immediately rather than at the
    end of a round: there is no barrier anywhere in this loop, because a barrier prices a
    group at its slowest member, and a wave of three where one takes an hour and two take
    ten minutes leaves two slots idle for fifty minutes with eligible work sitting there.
    Stated as the invariant it is, which is also what the tests assert: *at no point does
    an eligible, unclaimed child exist while a slot is free.*

    **Takeoff and landing are different resources, and only landing is serial.** The work
    parallelises because each child has its own worktree; the merge does not, because
    ``main`` is one branch -- so the children queue for the repository's runway inside
    their own ``agentjobs finish``. See :class:`agentjobs.dispatch.finish.Runway`. That
    queue is the honest cost of this and it is much smaller than the flights: on this
    machine's ledger a scripted finish is about four minutes against a run of about
    thirty.

    **It never closes the parent, and that is not an omission.** The task's own
    constraint is that a parent closes only when its acceptance criteria are supported by
    the children's evidence, and whether they are is the one judgement in this whole loop
    that is not mechanical. So a walk that gets to the end writes what every child did
    onto the parent's record and hands back; whoever called it -- the supervising session,
    or a person at a shell -- reads its own criteria against that and closes the parent,
    or does not.

    **One bad child stops every further takeoff, and that argument survives concurrency
    intact -- but only that argument.** A sibling that depended on the failed child would
    be building on a gap, and nobody is awake to notice. What the rule justifies is
    *stopping*, and stopping is not the same as never having started: a child already in
    the air cannot depend on the failed one, or claimability would not have offered it,
    so it is allowed to land. Killing it would throw work away for no safety gain.
    Anything that *did* need the failed child never enters the frontier at all, because
    its needs are unmet and always will be. What this gives up against the old serial
    rule is exactly the pessimism about siblings that provably do not depend on it.

    ``posture`` is a choice made *for this walk*, and it is the only posture input this
    function has (task-316). It is what ``agentjobs dispatch walk --posture`` supplies,
    and it exists because a walk started from a shell has no parent run whose envelope it
    could inherit -- the epic may never have been dispatched at all. Left ``None``, which
    is the case for every walk a supervising run starts, each child works its posture out
    for itself: the parent's dispatch-time choice if a person made one, then the child's
    own record, then the project default. Passing one refuses above the ceiling exactly
    as a dispatch-time choice does, because that is what it is.

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
            posture=posture,
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

    from agentjobs.dispatch.guards import ConcurrencyLimitError

    slots = max(1, settings.max_concurrent)
    started = 0
    in_flight: Dict[str, Flight] = {}
    retries: List[str] = []
    # Set the first time a child lands badly. From then on nothing further takes off, but
    # whatever is already in the air is watched down before the walk returns.
    grounded: Optional[Tuple[WalkStop, str]] = None
    # When the sky is empty and the machine will not give us a slot. Bounded by the
    # per-child ceiling, because a walk that can never start anything is the same kind of
    # "something upstream is not settling" that ceiling already exists for.
    blocked_since: Optional[float] = None

    def ground(stop: WalkStop, detail: str) -> None:
        nonlocal grounded
        if grounded is not None:
            return
        grounded = (stop, detail)
        if in_flight:
            announce(
                f"No further children will be started. {len(in_flight)} already in "
                f"flight ({', '.join(sorted(in_flight))}) will be watched down first -- "
                "none of them can depend on the one that stopped this, or it would not "
                "have been eligible."
            )

    while True:
        # **Every run status for this tick is read before the corpus snapshot it will be
        # judged against, and that ordering is a correctness argument rather than a
        # style.** A child writes its last word and *then* its process exits, so a record
        # read after a terminal status is guaranteed to contain that word. Read the other
        # way round, a child that finished in the millisecond between the two reads looks
        # dead, and the walk spends its retry re-doing work already on `main`.
        #
        # Serial walks got this by reading one status immediately before one refresh.
        # With several children the same guarantee needs every status taken first --
        # otherwise the second child's status is read after a snapshot the first child's
        # refresh took.
        statuses = {
            child_id: (status_of(flight.run_id) if flight.run_id else None)
            for child_id, flight in in_flight.items()
        }
        # Every fact this loop turns on is written by another process, and the CLI holds
        # one corpus snapshot for a whole invocation. Dropping it here is what makes the
        # walk's reads reads.
        manager.storage.refresh()

        # ----- land whatever has finished -------------------------------------
        for child_id in list(in_flight):
            flight = in_flight[child_id]
            attempt = _poll_child(
                manager=manager,
                flight=flight,
                settings=settings,
                status=statuses.get(child_id),
                now=now,
            )
            if attempt is None:
                continue
            del in_flight[child_id]
            result.attempts.append(attempt)
            announce(attempt.describe())
            if attempt.verdict.is_clean:
                continue
            if attempt.verdict.is_retryable and grounded is None:
                # The retry re-reads the attempt count off the record, so the bound is
                # enforced by the same code whether the retry happens here or in a walk
                # somebody starts tomorrow. Nothing is counted in memory.
                retries.append(child_id)
                continue
            ground(
                {
                    ChildVerdict.PARKED: WalkStop.CHILD_NEEDS_A_HUMAN,
                    ChildVerdict.CLOSED_UNRESOLVED: WalkStop.CHILD_CLOSED_UNRESOLVED,
                    ChildVerdict.TIMED_OUT: WalkStop.CHILD_TIMED_OUT,
                    ChildVerdict.DIED: WalkStop.CHILD_EXHAUSTED_ATTEMPTS,
                }[attempt.verdict],
                attempt.detail,
            )

        # ----- fill every free slot -------------------------------------------
        backpressure = False
        while grounded is None and len(in_flight) < slots:
            if settings.max_children is not None and started >= settings.max_children:
                break
            # A retry goes back to the same child rather than back to the frontier. It
            # has to: the first attempt claimed it, so it is `active` and claimability --
            # correctly -- will not offer a claimed task to anybody. Asking the frontier
            # again would report the epic as deadlocked on the very child about to be
            # tried again.
            if retries:
                candidate = manager.get_task(retries[0])
                if candidate is None:
                    retries.pop(0)
                    continue
            else:
                available = frontier(manager, parent_id, exclude=tuple(in_flight))
                if not available:
                    break
                candidate = available[0]

            authorization = resolve_epic_authorization(manager, project_config, candidate)
            try:
                assert_attempts_remain(authorization, candidate)
            except ChildAttemptsExhaustedError as exc:
                ground(WalkStop.CHILD_EXHAUSTED_ATTEMPTS, str(exc))
                break

            attempt_number = authorization.attempts_used + 1
            announce(
                f"Starting {candidate.id} (attempt {attempt_number} of "
                f"{CHILD_ATTEMPT_LIMIT}); {len(in_flight) + 1} of {slots} slots in use."
            )
            try:
                handle = start_child(candidate)
            except ConcurrencyLimitError as exc:
                # Backpressure, not a refusal about this child. Something else on the
                # machine holds a slot; that is a normal condition and the walk waits for
                # it exactly as it waits for a child.
                backpressure = True
                announce(f"Waiting for a run slot: {exc}")
                break
            except DispatchRefused as exc:
                ground(
                    WalkStop.COULD_NOT_START_CHILD,
                    f"{candidate.id} could not be started "
                    f"({getattr(exc, 'reason', 'refused')}): {exc}",
                )
                result.attempts.append(
                    ChildAttempt(
                        child_id=candidate.id,
                        attempt=attempt_number,
                        run_id=None,
                        verdict=ChildVerdict.DIED,
                        detail=str(exc),
                    )
                )
                break

            if retries:
                retries.pop(0)
            started += 1
            in_flight[candidate.id] = Flight(
                child_id=candidate.id,
                attempt=attempt_number,
                run_id=getattr(handle, "run_id", None),
                deadline=now() + settings.child_timeout_seconds,
            )
            result.peak_in_flight = max(result.peak_in_flight, len(in_flight))

        # ----- is there anything left to do? ----------------------------------
        if in_flight:
            blocked_since = None
            sleep(settings.poll_seconds)
            continue

        if grounded is not None:
            result.stop, result.detail = grounded
            return result

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

        if backpressure:
            if blocked_since is None:
                blocked_since = now()
            elif now() - blocked_since >= settings.child_timeout_seconds:
                result.stop = WalkStop.COULD_NOT_START_CHILD
                result.detail = (
                    f"Nothing of {parent_id} has been able to start for "
                    f"{settings.child_timeout_seconds / 3600:.1f}h: this machine's "
                    "concurrent-run ceiling has been full the whole time and none of "
                    "the runs holding it belong to this epic. Raise "
                    "`limits.max_concurrent_runs`, or cancel whatever is holding the "
                    "slots, and start the walk again."
                )
                return result
            sleep(settings.poll_seconds)
            continue

        result.stop = WalkStop.NO_ELIGIBLE_CHILD
        result.detail = (
            f"{len(remaining)} open child/children remain and none is claimable: "
            f"{', '.join(child.id for child in remaining)}. Each is blocked by an "
            "unmet dependency, already claimed, or holding open children of its own. "
            "This is not a finished epic and is reported separately from one."
        )
        return result


def _poll_child(
    *,
    manager: TaskManager,
    flight: Flight,
    settings: WalkSettings,
    status: Optional[str],
    now: Callable[[], float],
) -> Optional[ChildAttempt]:
    """One look at one child: its verdict, or ``None`` while it is still flying.

    ``ball`` is the signal, exactly as the workflow guide says: a child parked on review
    has a live process and is the one state that needs somebody, so a process-liveness
    check would report it as healthy for as long as anybody left it there. The run status
    is consulted for one question only -- *is the session still there* -- and only to
    tell a child that died apart from a child that is still thinking.

    **Non-blocking, because the walk watches several children at once** (task-223). The
    sleep belongs to the caller's loop rather than to this function; a blocking watcher
    per child would need a thread per child, and threads that each spawn dispatches and
    write task records are a much larger change than the scheduling this task is about.
    One loop, one poll interval, N children looked at per tick.

    ``status`` is this child's run status **as read before the caller's refresh**, and
    the caller owns that ordering -- see the comment at the top of the walk loop for why
    reading it here instead would reintroduce a race that has already cost real work.
    """
    from agentjobs.dispatch.guards import TERMINAL_RUN_STATUSES

    child_id = flight.child_id
    run_id = flight.run_id

    def verdict(kind: ChildVerdict, detail: str) -> ChildAttempt:
        return ChildAttempt(
            child_id=child_id,
            attempt=flight.attempt,
            run_id=run_id,
            verdict=kind,
            detail=detail,
        )

    child = manager.get_task(child_id)
    if child is None:
        return verdict(
            ChildVerdict.DIED, "The task record disappeared while the walk was watching it."
        )

    if child.lifecycle is Lifecycle.CLOSED:
        outcome = child.outcome
        if outcome is Outcome.COMPLETED:
            return verdict(
                ChildVerdict.COMPLETED,
                "closed completed; its own gate ran and its own merge happened",
            )
        return verdict(
            ChildVerdict.CLOSED_UNRESOLVED,
            f"closed with outcome {outcome.value if outcome else 'none'}, which is a "
            "deliberate act by whoever closed it and not something to walk past",
        )

    if child.ball is not Ball.AGENT:
        reason = child.ball_reason.value if child.ball_reason else "unstated"
        holder = child.ball.value if child.ball else "nobody"
        return verdict(
            ChildVerdict.PARKED,
            f"ball is {holder}/{reason}: {child.ball_prompt or 'no prompt recorded'}",
        )

    if status is not None and status in TERMINAL_RUN_STATUSES:
        return verdict(
            ChildVerdict.DIED,
            f"run ended {status!r} with the child still open and its ball still with the "
            "agent, so the session went without finishing",
        )

    if now() >= flight.deadline:
        return verdict(
            ChildVerdict.TIMED_OUT,
            f"nothing terminal happened in {settings.child_timeout_seconds / 3600:.1f}h. "
            "Something upstream is not settling this run; the walk stops rather than "
            "waiting for morning",
        )

    return None


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
            "**The first child that is not clean grounds every further takeoff, and no "
            "child is ever skipped.** A sibling that depended on it would be building on "
            "a gap, and nobody is awake to notice. Children already in flight were "
            "watched down rather than killed -- none of them could have depended on this "
            "one, or they would not have been eligible to start. Whatever the child's "
            "record says it needs is what this epic needs next.",
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


def describe_settings(
    settings: WalkSettings,
    *,
    posture: Optional[Posture] = None,
    inherited: Optional[Posture] = None,
) -> Sequence[str]:
    """The bounds, printed before a walk starts so nobody has to guess at them.

    The envelope line is not a bound and is printed anyway, for the reason task-308 gave
    for printing one after every dispatch: "what may these runs do, and who decided
    that" is the question this feature makes expensive to get wrong, and a line that
    appears only when something is unusual trains a reader to skim it. It says
    ``the project default`` when nothing overrides it, which is a claim about what will
    happen rather than an absence.
    """
    if posture is not None:
        envelope = f"{posture.value} (chosen for this walk)"
    elif inherited is not None:
        envelope = f"{inherited.value} (inherited from the epic's own dispatch)"
    else:
        envelope = "the project default, or each child's own record where it sets one"
    if settings.max_concurrent > 1:
        slots = (
            f"{settings.max_concurrent} at once, so independent children fly in parallel "
            "and queue for the merge runway"
        )
    else:
        slots = "1 at a time"
    return (
        f"attempts per child: {CHILD_ATTEMPT_LIMIT} (first run plus one retry)",
        f"poll interval: {settings.poll_seconds:.0f}s",
        f"per-child ceiling: {settings.child_timeout_seconds / 3600:.1f}h",
        f"children this walk may start: {settings.max_children or 'every open one'}",
        f"children in flight: {slots}",
        f"posture children start at: {envelope}",
    )
