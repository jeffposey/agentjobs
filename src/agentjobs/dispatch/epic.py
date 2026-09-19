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
actor is a human, which keeps agent-starts-agent out of every supported path. (It does not
make the cycle impossible, and section 2 no longer says it does -- ``dispatch/budget.py``
holds what actually bounds one.) A walk that starts five child runs cannot be allowed to
weaken the rule.

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

The runner crosses on the same terms (task-453). Task-316 carried the posture and
nothing else, so an epic dispatched on ``claude-fable-5-1`` through ``big-dawg`` on
2026-09-18 started its first child on ``claude-opus-5``: ``posture_source: epic`` on the
child's record, and the project default in its argv. :func:`inherited_runner` reads the
runner and group off the same parent entry the posture comes from, the guards resolve
the child's runner *as* that record or refuse -- never as today's default -- and
:data:`INHERITABLE_RUNNER_SOURCES` says which sources cross. The refusal is deliberate:
``big-dawg`` is single-member so that an unavailable model is a stop, not a substitution,
and the walk grounds on it with the reason on the record.

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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from agentjobs.actors import Actor
from agentjobs.dispatch.config import DispatchError, Posture, PostureSource, SelectionSource
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
from agentjobs.store_factory import TaskManagerLike

if TYPE_CHECKING:  # pragma: no cover - the journal is imported where it is used
    from agentjobs.execution.store import ExecutionStore, Supervision

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
    runner: Optional[str] = None
    """The runner the epic was dispatched with, when somebody chose one (task-453).

    Read the same way as the posture, off the parent's newest ``dispatch`` entry, and
    ``None`` on the same terms: a parent that ran on the project's default runner passes
    nothing down, because the default already reaches every child on its own. Set, the
    child is started on exactly this runner or refused -- never on whatever the default
    has become. See :func:`inherited_runner`.
    """
    group: Optional[str] = None
    """The group that runner was chosen from, when a group chose it. Carried so the
    refusal can say which group's member is no longer enabled."""

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
        attribution. The runner is named on the same terms (task-453): a child that ran
        on a model the project default does not name should say, in the entry that
        authorised it, that the model was the epic's.
        """
        envelope = (
            f" They chose posture `{self.posture.value}` for the epic, so this run gets " "it too."
            if self.posture is not None
            else ""
        )
        if self.runner is not None:
            via = f" from group `{self.group}`" if self.group else ""
            envelope += (
                f" The epic was dispatched on runner `{self.runner}`{via}, so this run is too."
            )
        return (
            f"{self.actor.display_name} authorised a dispatch of {self.parent.id}, and "
            f"this run is attempt {self.attempts_used + 1} of {CHILD_ATTEMPT_LIMIT} at "
            f"one of its children on that authorisation (entry {self.entry.id} on "
            f"{self.parent.id}).{envelope} No separate approval of this task was given "
            "or is required: the epic is what was authorised."
        )

    def data(self) -> Dict[str, object]:
        epic: Dict[str, object] = {
            "parent": self.parent.id,
            "entry": self.entry.id,
            "attempt": self.attempts_used + 1,
            "limit": CHILD_ATTEMPT_LIMIT,
        }
        if self.runner is not None:
            epic["runner"] = self.runner
            epic["group"] = self.group
        return {
            "authorizes_dispatch": True,
            "surface": "the epic walk",
            EPIC_DATA_KEY: epic,
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


INHERITABLE_RUNNER_SOURCES = frozenset(
    {
        SelectionSource.DISPATCH_RUNNER,
        SelectionSource.DISPATCH,
        SelectionSource.EPIC,
        SelectionSource.HISTORY,
    }
)
"""Which sources of a parent run's runner cross into its children (task-453).

The same principle as :data:`INHERITABLE_POSTURE_SOURCES`: what crosses the boundary is
a choice somebody made for *this epic*, never a default that reaches the child on its
own. ``dispatch_runner`` and ``dispatch`` are that choice -- a runner or a group named
when the epic was dispatched -- and ``epic`` is the same choice one generation down.

``history`` is in this set and not in the posture one, and the difference is deliberate.
A parent resumed after a handback is a continuation: its runner is frozen from the grant
it carries on (task-375), so the record says ``history`` even when the original grant was
a person naming ``big-dawg``. A walk run from that resumed parent would otherwise put its
children on the project default -- the task-410 shape again, reached through a resume
instead of a walk. Where the original grant *was* the project default, inheriting it
changes what the child runs on only if the default has moved since, and then keeping
the child on the parent's model is the deterministic answer section 9a asks for.

``project``, ``machine`` and ``project_runner`` do not cross, for the reason the posture
set gives: the default already reaches every child, and relabelling it ``epic`` would
point a reader at the parent for an answer that is in ``dispatch.yaml``.
"""


def inherited_runner(parent: Task) -> Optional[Tuple[str, Optional[str]]]:
    """The ``(runner, group)`` a child should be started on, or ``None`` to decide locally.

    Read off the parent's newest ``dispatch`` entry, the same one the posture and the
    authorisation come from, so a child runs on the runner of *the run that is walking
    it*. The entry records the source in one of two places: ``runner_source`` when a
    runner was named outright or carried from history, and ``selection.source`` when a
    group chose it. Either way the runner is the entry's ``runner`` field -- the member
    the group actually selected, not the group's first choice today -- and the group is
    the selection's, when there was one.

    ``None`` for a source outside :data:`INHERITABLE_RUNNER_SOURCES`, for an entry that
    records no source at all, and for an entry whose runner field is missing. Observed
    on 2026-09-18: a parent whose entry said ``runner: claude-fable-5-1`` and
    ``runner_source: dispatch_runner`` started its first child on ``claude-opus-5``,
    because nothing read this.
    """
    entry = parent_dispatch_entry(parent)
    if entry is None or not isinstance(entry.data, dict):
        return None
    runner = entry.data.get("runner")
    if not isinstance(runner, str) or not runner:
        return None
    group: Optional[str] = None
    raw_source = entry.data.get("runner_source")
    selection = entry.data.get("selection")
    if isinstance(selection, dict):
        recorded_group = selection.get("group")
        if isinstance(recorded_group, str) and recorded_group:
            group = recorded_group
        if raw_source is None:
            raw_source = selection.get("source")
    try:
        source = SelectionSource(raw_source)
    except ValueError:
        return None
    if source not in INHERITABLE_RUNNER_SOURCES:
        return None
    return runner, group


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


class Contention(Enum):
    """What a walk refused ``live_run_exists`` on a child can see of the run in its way."""

    OURS = "ours"
    """A live run the child's log says was dispatched on this epic's authorisation."""
    FOREIGN = "foreign"
    """A live run the child's log says was dispatched on something else."""
    PENDING = "pending"
    """Something holds the child and the record does not yet say what: a dispatch between
    its lock and its dispatch entry, which is seconds wide and says nothing either way."""
    CLEAR = "clear"
    """Nothing is live on the child any more."""


def _dispatched_on_this_epic(
    child: Task, run_id: str, *, parent_id: str, entry_id: int
) -> Optional[bool]:
    """Whether the child's log dispatched ``run_id`` on this epic's authorisation.

    ``None`` when no ``dispatch`` entry names the run yet -- a spawn still in progress.
    """
    by_id = {entry.id: entry for entry in child.log}
    for entry in child.log:
        data = entry.data if isinstance(entry.data, dict) else {}
        if entry.type is not LogEntryType.DISPATCH or data.get("run_id") != run_id:
            continue
        caused_by = data.get("caused_by")
        cause = by_id.get(caused_by) if isinstance(caused_by, int) else None
        marker = (cause.data or {}).get(EPIC_DATA_KEY) if cause is not None else None
        return (
            isinstance(marker, dict)
            and marker.get("parent") == parent_id
            and marker.get("entry") == entry_id
        )
    return None


def contention(
    child: Task,
    *,
    home: Path,
    project_id: str,
    parent_id: str,
    entry_id: int,
    known_runs: Sequence[str] = (),
) -> Tuple[Contention, Optional[str]]:
    """Whose run a child's ``live_run_exists`` refusal was about, and its id when known.

    Decided by the child's own log, not by which process launched the run (task-444): the
    authorisation is the fact that makes a run this epic's, and a sibling walk, a restarted
    one or a person at a shell dispatching on the same entry all leave the same trail -- a
    ``dispatch`` entry naming the run, caused by a note carrying this epic's marker.

    A child that has already **closed** is asked the same question of its log alone: the
    run that closed it may be over by now, and its verdict is still this epic's to land.
    ``known_runs`` are runs this walk has already landed, so an old attempt of its own is
    never mistaken for the one in its way.
    """
    from agentjobs.dispatch.journal import effective_live
    from agentjobs.dispatch.ledger import live_runs

    known = set(known_runs)
    if not child.is_open:
        for entry in reversed(child.log):
            data = entry.data if isinstance(entry.data, dict) else {}
            run_id = data.get("run_id")
            if entry.type is not LogEntryType.DISPATCH or not isinstance(run_id, str):
                continue
            if run_id in known:
                break
            if _dispatched_on_this_epic(child, run_id, parent_id=parent_id, entry_id=entry_id):
                return Contention.OURS, run_id
            break
        return Contention.CLEAR, None

    runs = [
        run
        for run in effective_live(home, live_runs(home))
        if run.task_id == child.id and run.project_id in (project_id, "")
    ]
    if not runs:
        return Contention.CLEAR, None
    for run in reversed(runs):
        verdict = _dispatched_on_this_epic(
            child, run.run_id, parent_id=parent_id, entry_id=entry_id
        )
        if verdict is True:
            return Contention.OURS, run.run_id
        if verdict is False:
            return Contention.FOREIGN, run.run_id
    return Contention.PENDING, runs[-1].run_id


def resolve_epic_authorization(
    manager: TaskManagerLike,
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
    runner = inherited_runner(parent)
    return EpicAuthorization(
        parent=parent,
        entry=entry,
        actor=actor,
        attempts_used=used,
        posture=inherited_posture(parent),
        runner=runner[0] if runner is not None else None,
        group=runner[1] if runner is not None else None,
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
    ALREADY_SUPERVISED = "already_supervised"
    """Another live supervisor holds this epic's walk. Two would launch every child twice."""
    NO_ELIGIBLE_CHILD = "no_eligible_child"
    """Open children remain and none of them is claimable -- every one is blocked,
    claimed elsewhere, or holding open children of its own. Not the same as being done,
    and reported differently so nobody reads a deadlocked graph as a finished epic."""

    @property
    def is_success(self) -> bool:
        return self is WalkStop.ALL_CHILDREN_DONE

    @property
    def is_a_wait(self) -> bool:
        """Whether this ending is something that clears on its own (task-467).

        A walk that stops for one of these is not stuck and has nothing to report to a
        person: a child parked for review is already in their list with a button on it,
        and a sibling that is claimed or mid-finish is somebody else's live work. The
        distinction decides two things -- that the parent is handed to
        ``external``/``dependency`` naming the blocker rather than asked a second
        question, and that the walk stays ``walking`` so an ordinary poll tick resumes
        the epic when the blocker lets go.

        ``NO_ELIGIBLE_CHILD`` is only conditionally a wait: it also covers a graph that
        is genuinely deadlocked, which clears for nobody. Whether this particular one
        waits is therefore decided by :func:`waiting_child`, against the live records,
        rather than by the enum member alone.
        """
        return self in (WalkStop.CHILD_NEEDS_A_HUMAN, WalkStop.NO_ELIGIBLE_CHILD)


@dataclass
class WalkResult:
    """Everything the walk did, in the order the children *landed*."""

    parent_id: str
    stop: WalkStop
    attempts: List[ChildAttempt] = field(default_factory=list)
    detail: str = ""
    stopped_on: Optional[str] = None
    """The child that grounded this walk, when one did.

    Recorded rather than derived, because deriving it was wrong (task-466). The handoff
    took ``attempts[-1]`` and called it the failing child, but attempts are ordered by
    when each child *landed*, and since task-223 the walk watches down whatever is
    already in the air after it grounds. The last lander is therefore routinely a child
    that succeeded: on 2026-09-18 task-212's handoff told the owner the walk had stopped
    on task-464 while quoting task-465's reason and run id, and task-464 had merged
    cleanly. A record naming the wrong task is worse than one naming none.
    """
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


def next_eligible_child(manager: TaskManagerLike, parent_id: str) -> Optional[Task]:
    """The child the queue says is next, or ``None`` if none is claimable."""
    return manager.get_next_task(parent=parent_id)


def frontier(
    manager: TaskManagerLike, parent_id: str, *, exclude: Sequence[str] = ()
) -> List[Task]:
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


def starts_a_walk(manager: TaskManagerLike, task_id: str) -> bool:
    """Whether dispatching this task hands an epic to the server rather than starting an agent.

    One open child is enough, which is the same property ``get_next_task()`` uses to refuse
    to hand out a parent and the same one the prompt stub used before task-458: it needs no
    field, no label anybody has to remember to set, and no judgement at spawn time.

    A task whose children cannot be listed is dispatched as an ordinary task rather than
    not dispatched at all, which is the reading ``DispatchRunner.open_child_ids`` has always
    taken of the same failure: this decorates a dispatch, and must never be able to refuse
    one.
    """
    try:
        return any(child.is_open for child in manager.get_subtasks(task_id))
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def open_children(manager: TaskManagerLike, parent_id: str) -> List[Task]:
    children = manager.get_subtasks(parent_id)
    return [child for child in children if child.is_open]


def waiting_child(
    manager: TaskManagerLike,
    parent_id: str,
    stop: "WalkStop",
    stopped_on: Optional[str] = None,
) -> Optional[Task]:
    """The open child this ending is merely waiting on, or ``None`` if it is a real stop.

    The whole of task-467's first clause lives here. A walk that grounded because a
    child parked for review is waiting on a person who already has that child in their
    list with a button on it; asking them a second question about the parent is how one
    click came to read as two asks. A walk that found nothing eligible because the only
    open child is somebody else's live work is waiting too -- and handing *that* to a
    person solicits the one action that cannot help.

    ``NO_ELIGIBLE_CHILD`` is the interesting case, and it is why this is a function
    against the live records rather than a property of the enum. The same member also
    covers a graph in which every remaining child is blocked by a dependency that
    nothing is working on, which clears for nobody and is exactly when a person should
    be asked. So a wait is claimed only when some open child is actually holding the
    ball: a person for a review, or an agent that has claimed it.
    """
    if not stop.is_a_wait:
        return None
    children = open_children(manager, parent_id)
    if stop is WalkStop.CHILD_NEEDS_A_HUMAN:
        named = next((child for child in children if child.id == stopped_on), None)
        if named is not None and named.ball is Ball.HUMAN:
            return named
        return next((child for child in children if child.ball is Ball.HUMAN), None)
    held_by_person = next((child for child in children if child.ball is Ball.HUMAN), None)
    if held_by_person is not None:
        return held_by_person
    return next(
        (
            child
            for child in children
            if child.ball is Ball.AGENT and child.lifecycle is Lifecycle.ACTIVE
        ),
        None,
    )


def grounding_cleared(
    manager: TaskManagerLike, stop: "WalkStop", stopped_on: Optional[str]
) -> bool:
    """Whether a recorded wait has stopped being true, so the walk may take off again.

    Asked once per tick, of a walk rebuilt from its record. It is the counterpart to
    :func:`waiting_child`: that one decides whether to keep waiting, this one decides
    whether there is anything left to wait for.

    A child that **closed unresolved** does not clear anything. It was parked on a
    person, and the person cancelled it rather than finishing it -- a sibling that would
    now take off could be building on the gap that cancellation left, which is precisely
    what grounding exists to prevent. That ending is a stop for cause and is reported
    as one.
    """
    if stop is WalkStop.NO_ELIGIBLE_CHILD:
        # Re-derived from the frontier on every tick anyway, so a remembered one says
        # nothing the next pass will not say better.
        return True
    if stop is not WalkStop.CHILD_NEEDS_A_HUMAN:
        return False
    if not stopped_on:
        return False
    child = manager.get_task(stopped_on)
    if child is None:
        return False
    if child.is_open:
        return child.ball is not Ball.HUMAN
    return child.outcome is Outcome.COMPLETED


@dataclass
class Flight:
    """One child currently in the air: what was started, and when to give up on it."""

    child_id: str
    attempt: int
    run_id: Optional[str]
    deadline: float


SETTLE_GRACE_SECONDS = 120.0
"""How long a landed child's still-live run is counted against the walk's slots.

A child's record closes before its session settles -- task-375 closed at 18:01:17 UTC and
its run settled at 18:01:46 -- and a child started into that gap is refused by the machine
ceiling. Bounded, because a run that never settles is a poller problem and must not park
the walk; after this the walk tries and treats a refusal as backpressure, as before."""

CONTENTION_GRACE_SECONDS = 300.0
"""How long a child refused ``live_run_exists`` may stay unattributable before it grounds.

Somebody else's dispatch of the child is between its lock and the dispatch entry that
names its run -- a spawn, seconds to a minute. Longer than that with no record saying
whose run it is means nothing is going to say, and the walk stops as it always did rather
than waiting on a holder it cannot identify (task-444)."""


def _revision(manager: TaskManagerLike, task_id: str) -> Optional[str]:
    task = manager.get_task(task_id)
    return task.updated.isoformat() if task is not None else None


def _eligible(manager: TaskManagerLike, parent_id: str, in_flight: Dict[str, "Flight"]) -> bool:
    return bool(frontier(manager, parent_id, exclude=tuple(in_flight)))


@dataclass
class _Restored:
    in_flight: Dict[str, "Flight"] = field(default_factory=dict)
    retries: List[str] = field(default_factory=list)
    attempts: List["ChildAttempt"] = field(default_factory=list)
    grounded: Optional[Tuple["WalkStop", str]] = None
    grounded_on: Optional[str] = None
    """The child the restored grounding names, so a resumed walk hands off saying it."""
    started: int = 0
    peak_in_flight: int = 0
    notes: List[str] = field(default_factory=list)


class _Supervision:
    """The walk's durable record in the execution journal (task-416, from task-418).

    Every method commits before the walk acts on what it recorded, and every one is
    fenced by the walk's epoch: a supervisor that was taken over cannot write over the one
    that took it.
    """

    def __init__(
        self,
        store: "ExecutionStore",
        walk: "Supervision",
        *,
        timeout: float,
        wall: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.walk = walk
        self.timeout = timeout
        self.wall = wall
        self.refusal: Optional[str] = None

    @classmethod
    def open(
        cls,
        *,
        manager: TaskManagerLike,
        project: Project,
        parent_id: str,
        home: Optional[Path],
        settings: "WalkSettings",
        host: str,
        wall: Optional[Callable[[], datetime]],
        posture: Optional[Posture] = None,
        actor: Optional[str] = None,
    ) -> Optional["_Supervision"]:
        if home is None:
            return None
        from agentjobs.dispatch.journal import journal
        from agentjobs.dispatch.ledger import process_alive
        from agentjobs.execution.errors import ExecutionStoreError, OwnershipConflict

        parent = manager.get_task(parent_id)
        entry = parent_authorizing_entry(parent) if parent is not None else None
        if entry is None:
            return None  # nothing authorises children; the walk refuses them as before
        clock = wall or (lambda: datetime.now(timezone.utc))
        try:
            store = journal(home)
            walk, _resumed = store.open_walk(
                project_id=project.id,
                parent_task_id=parent_id,
                authority_entry=entry.id,
                authority_actor=entry.actor,
                settings={
                    "max_concurrent": settings.max_concurrent,
                    "max_children": settings.max_children,
                    "child_timeout_seconds": settings.child_timeout_seconds,
                    # Saved even for a walk this process intends to finish itself
                    # (task-467): a walk that ends on a wait hands its record to the
                    # server, and the tick that picks it up has no other way to learn
                    # which envelope its children were authorised under or whose name
                    # the outcome is written in.
                    "posture": posture.value if posture is not None else None,
                    "actor": actor,
                },
                host=host,
                holder_alive=process_alive,
            )
        except OwnershipConflict as exc:
            refused = cls.__new__(cls)
            refused.refusal = str(exc)
            return refused
        except ExecutionStoreError:
            return None  # an unwritable journal walks as the pre-durable build did
        return cls(store, walk, timeout=settings.child_timeout_seconds, wall=clock)

    def restore(self, *, now: Callable[[], float]) -> _Restored:
        """Rebuild the walk from its record, reconciling every child before any takeoff."""
        restored = _Restored(started=self.walk.started, peak_in_flight=self.walk.peak_in_flight)
        grounding = self.walk.grounding
        if grounding:
            try:
                restored.grounded = (
                    WalkStop(grounding.get("stop")),
                    str(grounding.get("detail") or ""),
                )
                child = grounding.get("child")
                restored.grounded_on = str(child) if child else None
            except ValueError:
                restored.grounded = (WalkStop.CHILD_NEEDS_A_HUMAN, str(grounding))
        for child in self.store.supervised_children(self.walk.walk_id):
            for landed in child.history:
                try:
                    verdict = ChildVerdict(landed.get("verdict"))
                except ValueError:
                    continue
                restored.attempts.append(
                    ChildAttempt(
                        child_id=child.child_task_id,
                        attempt=int(landed.get("attempt") or 0),
                        run_id=landed.get("run_id"),
                        verdict=verdict,
                        detail=str(landed.get("detail") or ""),
                    )
                )
            if child.status == "admitting":
                admitted = (
                    self.store.attempt_by_operation(child.operation_id)
                    if child.operation_id
                    else None
                )
                if admitted is None:
                    # The admission never committed, and the process that reserved it is
                    # gone (or this walk would not have been resumable): nothing was paid
                    # for, so the reservation goes back.
                    self.store.record_child(
                        self.walk.walk_id,
                        epoch=self.walk.epoch,
                        child_task_id=child.child_task_id,
                        status="retry_owed",
                        refund=True,
                    )
                    restored.notes.append(
                        f"Resumed: {child.child_task_id}'s admission never committed; its "
                        "reserved attempt was returned."
                    )
                    if child.history:
                        restored.retries.append(child.child_task_id)
                    continue
                self._fly(child.child_task_id, admitted.run_id, admitted.execution_id)
                restored.notes.append(
                    f"Resumed: {child.child_task_id} was already admitted as {admitted.run_id}; "
                    "following it rather than starting it again."
                )
                restored.in_flight[child.child_task_id] = Flight(
                    child_id=child.child_task_id,
                    attempt=child.attempts_reserved,
                    run_id=admitted.run_id,
                    deadline=now() + self.timeout,
                )
            elif child.status == "flying":
                restored.in_flight[child.child_task_id] = Flight(
                    child_id=child.child_task_id,
                    attempt=child.attempts_reserved,
                    run_id=child.run_id,
                    deadline=now() + self._remaining(child.deadline_at),
                )
                restored.notes.append(f"Resumed: watching {child.child_task_id} ({child.run_id}).")
            elif child.status == "retry_owed" and child.history:
                restored.retries.append(child.child_task_id)
        return restored

    def adopt_unrecorded(
        self,
        manager: TaskManagerLike,
        parent_id: str,
        home: Path,
        *,
        exclude: set,
        now: Callable[[], float],
    ) -> List[Tuple["Flight", str]]:
        """Children already flying on this authorisation that the walk's record does not hold.

        A walk started by a build before this record existed, or one whose record was
        written by a supervisor on another host, leaves children `active` with a live run.
        Claimability rightly never offers a claimed task, so a restarted walk that trusted
        only the frontier saw nothing eligible and exited `no_eligible_child` -- leaving the
        flying children unwatched and the ones waiting on them unstarted (task-416 entry 19).
        They are adopted into the record here, before the frontier is asked anything.

        Only a child with a live run *and* an attempt on this exact authorisation is
        adopted; anything else is somebody else's work and is left alone.
        """
        from agentjobs.dispatch.journal import effective_live
        from agentjobs.dispatch.ledger import live_runs

        parent = manager.get_task(parent_id)
        entry = parent_authorizing_entry(parent) if parent is not None else None
        if entry is None:
            return []
        live = effective_live(home, live_runs(home))
        adopted: List[Tuple[Flight, str]] = []
        for child in open_children(manager, parent_id):
            if child.id in exclude or child.lifecycle is not Lifecycle.ACTIVE:
                continue
            attempts = count_attempts(child, parent_id=parent_id, entry_id=entry.id)
            if attempts == 0:
                continue
            runs = [
                run
                for run in live
                if run.task_id == child.id and run.project_id == self.walk.project_id
            ]
            if not runs:
                continue
            run = runs[-1]
            if self.store.supervised_child(self.walk.walk_id, child.id) is None:
                reserved = self.store.reserve_child_attempt(
                    self.walk.walk_id,
                    epoch=self.walk.epoch,
                    child_task_id=child.id,
                    operation_id=f"{self.walk.walk_id}:{child.id}:adopted:{run.run_id}",
                    limit=CHILD_ATTEMPT_LIMIT + 1,
                    used_on_record=attempts - 1,
                )
                if reserved is None:
                    continue
            attempt = self.store.attempt(run.run_id)
            self._fly(child.id, run.run_id, attempt.execution_id if attempt else None)
            adopted.append(
                (
                    Flight(
                        child_id=child.id,
                        attempt=attempts,
                        run_id=run.run_id,
                        deadline=now() + self.timeout,
                    ),
                    f"Adopted {child.id}: already flying as {run.run_id} on this authorisation.",
                )
            )
        return adopted

    def _remaining(self, deadline_at: Optional[str]) -> float:
        if not deadline_at:
            return self.timeout
        try:
            deadline = datetime.fromisoformat(deadline_at)
        except ValueError:
            return self.timeout
        return (deadline - self.wall()).total_seconds()

    def _fly(self, child_id: str, run_id: Optional[str], execution_id: Optional[str]) -> None:
        from datetime import timedelta

        self.store.record_child(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            child_task_id=child_id,
            status="flying",
            run_id=run_id,
            execution_id=execution_id,
            deadline_at=self.wall() + timedelta(seconds=self.timeout),
        )

    def adopt(self, child_id: str, run_id: str) -> None:
        """Follow a child another dispatch started on this authorisation (task-444).

        The walk reserved and refunded an attempt for it on the way to being refused, so
        the child already has a row; recording it flying is all adoption needs. Its attempt
        count is the child's log, which already holds the other dispatch's entry.
        """
        attempt = self.store.attempt(run_id)
        self._fly(child_id, run_id, attempt.execution_id if attempt else None)

    def reserve(self, child_id: str, used_on_record: int) -> Optional[Tuple[int, str]]:
        child = self.store.supervised_child(self.walk.walk_id, child_id)
        attempt = max(used_on_record, child.attempts_reserved if child else 0) + 1
        operation_id = f"{self.walk.walk_id}:{child_id}:{attempt}"
        reserved = self.store.reserve_child_attempt(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            child_task_id=child_id,
            operation_id=operation_id,
            limit=CHILD_ATTEMPT_LIMIT,
            used_on_record=used_on_record,
        )
        if reserved is None:
            return None
        return reserved.attempts_reserved, operation_id

    def refuse(self, child_id: str, operation_id: Optional[str], *, grounded: bool = False) -> None:
        """A dispatch raised: keep the reservation only if the admission committed."""
        spent = (
            bool(operation_id) and self.store.attempt_by_operation(operation_id or "") is not None
        )
        self.store.record_child(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            child_task_id=child_id,
            status="grounded" if grounded else "retry_owed",
            refund=not spent,
        )

    def take_off(self, flight: "Flight", *, started: int, peak: int) -> None:
        execution_id = None
        if flight.run_id:
            attempt = self.store.attempt(flight.run_id)
            execution_id = attempt.execution_id if attempt is not None else None
        self._fly(flight.child_id, flight.run_id, execution_id)
        self.store.update_walk(
            self.walk.walk_id, epoch=self.walk.epoch, started=started, peak_in_flight=peak
        )

    def land(self, attempt: "ChildAttempt", *, status: str, revision: Optional[str]) -> None:
        self.store.record_child(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            child_task_id=attempt.child_id,
            status=status,
            landed={
                "attempt": attempt.attempt,
                "run_id": attempt.run_id,
                "verdict": attempt.verdict.value,
                "detail": attempt.detail,
                "revision": revision,
                "observed_at": self.wall().isoformat(),
            },
        )

    def ground(self, stop: "WalkStop", detail: str, child: Optional[str] = None) -> None:
        self.store.update_walk(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            grounding={"stop": stop.value, "detail": detail, "child": child},
        )

    def finish(self, result: "WalkResult", *, waiting: bool = False) -> None:
        """Close the walk's record -- unless it is only waiting (task-467).

        A walk that stopped because a child is parked on a person, or because the one
        remaining child is somebody else's live work, has not finished: it has nothing
        to do *this tick*. Writing ``stopped`` there is what left task-421 with no
        supervisor at all, so that approving its child fired nothing and the epic never
        moved again. Such a walk stays ``walking`` and is handed to the server, whose
        poll tick rebuilds it from this record and takes off the moment the blocker lets
        go. The host moves to ``server`` with it, which is also what lets the next tick
        adopt the walk rather than refuse it: ownership is only defended for a *process*
        holder that is still alive, and this process is about to return.
        """
        if waiting:
            self.store.update_walk(
                self.walk.walk_id,
                epoch=self.walk.epoch,
                state="walking",
                host="server",
                stop=result.stop.value,
                detail=result.detail[:2000],
            )
            return
        self.store.update_walk(
            self.walk.walk_id,
            epoch=self.walk.epoch,
            state="done" if result.stop.is_success else "stopped",
            stop=result.stop.value,
            detail=result.detail[:2000],
        )

    def lift_grounding(self) -> None:
        """Forget a grounding whose cause has cleared, so the walk may take off again.

        The only caller is the wait above. Grounding is sticky everywhere else because
        the first cause is the one worth keeping; a wait is the one cause that stops
        being true without anybody deciding it has.
        """
        self.store.update_walk(self.walk.walk_id, epoch=self.walk.epoch, clear_grounding=True)


def _walk_epic(
    *,
    manager: TaskManagerLike,
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
    durable: Optional[bool] = None,
    once: bool = False,
    host: str = "process",
    wall: Optional[Callable[[], datetime]] = None,
    actor: Optional[str] = None,
) -> Optional[WalkResult]:
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

    **It is durable when it has a machine home** (task-416, absorbed from task-418): the
    authority it walks on, each child's reserved attempts and admission id, what is in
    flight and why the walk grounded are rows in the execution journal, committed before
    the act they describe. A walk started again on the same authorisation -- after its
    process died, or on the next server tick -- resumes that record instead of beginning
    again: it finds an admitted child by its admission id rather than dispatching it twice,
    returns a reservation a dispatch provably never used, and stays grounded. ``durable``
    overrides the default; ``once`` runs a single step and returns ``None`` while the walk
    is still going, which is how the server hosts a walk no session is waiting on.

    Injection points exist for the tests and for nothing else: ``dispatch``,
    ``read_run_status``, ``sleep``, ``now`` and ``wall``. A walk is a loop over processes that cost
    money and take an hour, and the alternative to injecting them is a test suite that
    proves the stop rule by not testing it.
    """
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
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

    def start_child(child: Task, operation_id: Optional[str]) -> object:
        request = DispatchRequest(
            task_id=child.id,
            trigger=DispatchTrigger.CHILD,
            on_behalf_of_parent=True,
            posture=posture,
            admission_operation_id=operation_id,
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

    from agentjobs.dispatch.guards import (
        AlreadyAdmittedError,
        ConcurrencyLimitError,
        LiveRunExistsError,
    )

    slots = max(1, settings.max_concurrent)
    started = 0
    in_flight: Dict[str, Flight] = {}
    retries: List[str] = []
    # Children refused `live_run_exists` whose run the record does not yet attribute, and
    # when that was first seen (task-444). Neither started nor dead: something else holds
    # them, and whether it is this epic's run decides between following it and grounding.
    contended: Dict[str, float] = {}
    # When each child was first refused that way. Kept across a holder letting go and
    # grabbing again, so the grace below bounds the whole episode rather than restarting.
    first_contended: Dict[str, float] = {}
    # Children that landed while their run still holds a machine slot (task-416,
    # from-walk-slots): a child's record closes before its session settles, and starting
    # the next child into that gap is refused by the machine ceiling every time.
    settling: Dict[str, Tuple[str, float]] = {}
    # Set the first time a child lands badly. From then on nothing further takes off, but
    # whatever is already in the air is watched down before the walk returns.
    grounded: Optional[Tuple[WalkStop, str]] = None
    # Which child that was. Held beside `grounded` rather than inside it because the
    # handoff a person reads names this task, and deriving it from the attempt order
    # named the wrong one -- see WalkResult.stopped_on.
    grounded_on: Optional[str] = None
    # When the sky is empty and the machine will not give us a slot. Bounded by the
    # per-child ceiling, because a walk that can never start anything is the same kind of
    # "something upstream is not settling" that ceiling already exists for.
    blocked_since: Optional[float] = None

    # ----- the durable record (task-416 part 2) ------------------------------
    supervision = _Supervision.open(
        manager=manager,
        project=project,
        parent_id=parent_id,
        home=ledger_home if (durable if durable is not None else home is not None) else None,
        settings=settings,
        host=host,
        wall=wall,
        posture=posture,
        actor=actor,
    )
    if supervision is not None and supervision.refusal is not None:
        result.stop = WalkStop.ALREADY_SUPERVISED
        result.detail = supervision.refusal
        return result
    if supervision is not None:
        restored = supervision.restore(now=now)
        in_flight.update(restored.in_flight)
        retries.extend(restored.retries)
        result.attempts.extend(restored.attempts)
        grounded = restored.grounded
        grounded_on = restored.grounded_on
        if (
            grounded is not None
            and grounded[0].is_a_wait
            and grounding_cleared(manager, grounded[0], grounded_on)
        ):
            # task-467: the one grounding that is allowed to be forgotten. The child
            # this walk stopped on has been resolved, so the reason no longer exists
            # and the epic may move again -- which is the whole point of the walk
            # having stayed alive instead of writing `stopped` and leaving nobody
            # watching.
            supervision.lift_grounding()
            announce(
                f"Resumed: the wait on {grounded_on or 'a child it did not name'} has "
                "cleared, so this walk is taking off again."
            )
            _resume_parent(manager, parent_id, actor=actor)
            grounded = None
            grounded_on = None
        started = restored.started
        result.peak_in_flight = restored.peak_in_flight
        for line in restored.notes:
            announce(line)
        for flight, note in supervision.adopt_unrecorded(
            manager, parent_id, ledger_home, exclude=set(in_flight), now=now
        ):
            in_flight[flight.child_id] = flight
            announce(note)

    def ground(stop: WalkStop, detail: str, child: Optional[str] = None) -> None:
        nonlocal grounded, grounded_on
        if grounded is not None:
            return
        grounded = (stop, detail)
        grounded_on = child
        if supervision is not None:
            # Committed before anything else happens, so a supervisor that dies in the
            # next instant restarts grounded rather than taking off again.
            supervision.ground(stop, detail, child)
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
        from agentjobs.dispatch.guards import TERMINAL_RUN_STATUSES

        for child_id, (run_id, since) in list(settling.items()):
            settled = status_of(run_id)
            if (
                settled is None
                or settled in TERMINAL_RUN_STATUSES
                or now() - since >= SETTLE_GRACE_SECONDS
            ):
                del settling[child_id]
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
            observed = statuses.get(child_id)
            if flight.run_id and observed is not None and observed not in TERMINAL_RUN_STATUSES:
                settling[child_id] = (flight.run_id, now())
            retry = attempt.verdict.is_retryable and grounded is None
            if supervision is not None:
                supervision.land(
                    attempt,
                    status="landed"
                    if attempt.verdict.is_clean
                    else ("retry_owed" if retry else "grounded"),
                    revision=_revision(manager, child_id),
                )
            if attempt.verdict.is_clean:
                continue
            if retry:
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
                attempt.child_id,
            )

        # ----- follow whatever another dispatch started for us ----------------
        for child_id, since in list(contended.items()):
            if grounded is not None:
                del contended[child_id]
                continue
            held = manager.get_task(child_id)
            epic_parent = manager.get_task(parent_id)
            authority = parent_authorizing_entry(epic_parent) if epic_parent else None
            if held is None or authority is None:
                # Gone, or the epic lost its authorisation: nothing to follow, and the
                # frontier and the refusals below say what comes next.
                del contended[child_id]
                continue
            whose, holder_run = contention(
                held,
                home=ledger_home,
                project_id=project.id,
                parent_id=parent_id,
                entry_id=authority.id,
                known_runs=[a.run_id for a in result.attempts if a.run_id],
            )
            if whose is Contention.OURS and holder_run:
                del contended[child_id]
                flight = Flight(
                    child_id=child_id,
                    attempt=count_attempts(held, parent_id=parent_id, entry_id=authority.id),
                    run_id=holder_run,
                    deadline=now() + settings.child_timeout_seconds,
                )
                in_flight[child_id] = flight
                result.peak_in_flight = max(result.peak_in_flight, len(in_flight))
                if supervision is not None:
                    supervision.adopt(child_id, holder_run)
                announce(
                    f"Adopted {child_id}: already flying as {holder_run} on this epic's "
                    "authorisation, started by another dispatch. Watching it rather than "
                    "starting a second."
                )
            elif whose is Contention.CLEAR and now() - since < CONTENTION_GRACE_SECONDS:
                # The holder let go, or has not yet got as far as a run: the child is the
                # frontier's again. Its first refusal's time is kept, so a lock that keeps
                # refusing with nothing behind it still grounds once the grace is spent.
                del contended[child_id]
            elif whose is Contention.FOREIGN:
                del contended[child_id]
                ground(
                    WalkStop.COULD_NOT_START_CHILD,
                    f"{child_id} is being worked by run {holder_run}, which was not dispatched "
                    "on this epic's authorisation. It is somebody else's run, so the walk "
                    "neither follows it nor starts a second.",
                    child_id,
                )
            elif now() - since >= CONTENTION_GRACE_SECONDS:
                del contended[child_id]
                ground(
                    WalkStop.COULD_NOT_START_CHILD,
                    f"{child_id} has been held by another dispatch"
                    + (f" (run {holder_run})" if holder_run else "")
                    + f" for {CONTENTION_GRACE_SECONDS / 60:.0f} minutes without its "
                    "record saying whose authorisation it runs on.",
                    child_id,
                )

        # ----- fill every free slot -------------------------------------------
        backpressure = False
        while grounded is None and len(in_flight) + len(settling) < slots:
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
                available = frontier(
                    manager, parent_id, exclude=tuple(in_flight) + tuple(contended)
                )
                if not available:
                    break
                candidate = available[0]

            authorization = resolve_epic_authorization(manager, project_config, candidate)
            try:
                assert_attempts_remain(authorization, candidate)
            except ChildAttemptsExhaustedError as exc:
                ground(WalkStop.CHILD_EXHAUSTED_ATTEMPTS, str(exc), candidate.id)
                break

            attempt_number = authorization.attempts_used + 1
            operation_id: Optional[str] = None
            if supervision is not None:
                # Reserved, with the admission's stable id, before the dispatch: a
                # supervisor that dies after the child's admission finds that attempt by
                # this id instead of admitting a second one, and one that dies before it
                # gets the reservation back (task-418's crash window).
                reserved = supervision.reserve(candidate.id, authorization.attempts_used)
                if reserved is None:
                    ground(
                        WalkStop.CHILD_EXHAUSTED_ATTEMPTS,
                        f"{candidate.id} has used its {CHILD_ATTEMPT_LIMIT} attempts on this "
                        "authorisation, counting attempts this walk reserved before a restart.",
                        candidate.id,
                    )
                    break
                attempt_number, operation_id = reserved
            announce(
                f"Starting {candidate.id} (attempt {attempt_number} of "
                f"{CHILD_ATTEMPT_LIMIT}); {len(in_flight) + len(settling) + 1} of {slots} "
                "slots in use."
            )
            try:
                handle = start_child(candidate, operation_id)
            except AlreadyAdmittedError as exc:
                handle = exc.attempt
            except ConcurrencyLimitError as exc:
                # Backpressure, not a refusal about this child. Something else on the
                # machine holds a slot; that is a normal condition and the walk waits for
                # it exactly as it waits for a child.
                if supervision is not None:
                    supervision.refuse(candidate.id, operation_id)
                backpressure = True
                announce(f"Waiting for a run slot: {exc}")
                break
            except LiveRunExistsError as exc:
                # Not a death and not yet a grounding (task-444). Something already holds
                # this child -- on 2026-09-13 a second walk of the same epic, whose healthy
                # child this walk recorded as dead and grounded the epic on. Whose run it
                # is decides, and the next pass asks the child's record.
                if supervision is not None:
                    supervision.refuse(candidate.id, operation_id)
                if retries and retries[0] == candidate.id:
                    retries.pop(0)
                contended[candidate.id] = first_contended.setdefault(candidate.id, now())
                announce(
                    f"{candidate.id} is already held by another dispatch; following its "
                    f"record to see whose run it is. ({exc})"
                )
                continue
            except DispatchError as exc:
                # `DispatchRefused` and the configuration refusals alike (task-453). The
                # one that matters here is `recorded_runner_unavailable`: the epic's
                # runner has been switched off since it was dispatched, and the walk
                # stops with that reason rather than starting the child on whatever
                # the default has become. Before this the config family escaped the
                # walk uncaught, which ended the supervisor with no record of why.
                if supervision is not None:
                    supervision.refuse(candidate.id, operation_id, grounded=True)
                ground(
                    WalkStop.COULD_NOT_START_CHILD,
                    f"{candidate.id} could not be started "
                    f"({getattr(exc, 'reason', 'refused')}): {exc}",
                    candidate.id,
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
            flight = Flight(
                child_id=candidate.id,
                attempt=attempt_number,
                run_id=getattr(handle, "run_id", None),
                deadline=now() + settings.child_timeout_seconds,
            )
            in_flight[candidate.id] = flight
            result.peak_in_flight = max(result.peak_in_flight, len(in_flight))
            if supervision is not None:
                supervision.take_off(flight, started=started, peak=result.peak_in_flight)

        def finish(*, waiting: bool = False) -> WalkResult:
            if supervision is not None:
                supervision.finish(result, waiting=waiting)
            return result

        # ----- is there anything left to do? ----------------------------------
        if (
            in_flight
            or (contended and grounded is None)
            or (
                settling
                and grounded is None
                and not backpressure
                and _eligible(manager, parent_id, in_flight)
            )
        ):
            blocked_since = None
            if once:
                return None
            sleep(settings.poll_seconds)
            continue

        if grounded is not None:
            result.stop, result.detail = grounded
            result.stopped_on = grounded_on
            return finish(
                waiting=waiting_child(manager, parent_id, result.stop, result.stopped_on)
                is not None
            )

        remaining = open_children(manager, parent_id)
        if not remaining:
            result.stop = WalkStop.ALL_CHILDREN_DONE
            result.detail = (
                f"No open child of {parent_id} remains. The parent is deliberately left "
                "open: whether its own acceptance criteria are met is a judgement this "
                "walk does not make."
            )
            return finish()

        if settings.max_children is not None and started >= settings.max_children:
            result.stop = WalkStop.NO_ELIGIBLE_CHILD
            result.detail = (
                f"Stopped after {started} child/children because --max-children said to. "
                f"{len(remaining)} open child/children remain: "
                f"{', '.join(child.id for child in remaining)}."
            )
            return finish()

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
                return finish()
            if once:
                return None
            sleep(settings.poll_seconds)
            continue

        result.stop = WalkStop.NO_ELIGIBLE_CHILD
        result.detail = (
            f"{len(remaining)} open child/children remain and none is claimable: "
            f"{', '.join(child.id for child in remaining)}. Each is blocked by an "
            "unmet dependency, already claimed, or holding open children of its own. "
            "This is not a finished epic and is reported separately from one."
        )
        return finish(
            waiting=waiting_child(manager, parent_id, result.stop, result.stopped_on) is not None
        )


def walk_epic(**kwargs: Any) -> WalkResult:
    """Walk an epic to its end. See :func:`_walk_epic` for everything it does.

    A single step (``once``) is :func:`advance_hosted_walks`' business and is not offered
    here, which is what lets every caller of this function rely on getting a result.
    """
    if kwargs.pop("once", False):
        raise TypeError("walk_epic runs a walk to its end; a single step is advance_hosted_walks")
    result = _walk_epic(**kwargs)
    assert result is not None  # only a single step returns before the walk ends
    return result


RECOVERING_ACTIONS = frozenset({"park", "notify"})
"""Auth-recovery handoffs that mean "this resumes without a person deciding anything".
An ``escalate`` is not one of them: it is recovery saying it cannot proceed."""


def _recovering(child: object, status: Optional[str]) -> bool:
    """Whether the child's newest handoff is an auth-recovery park its run is still under."""
    from agentjobs.dispatch.auth_recovery import MARKER
    from agentjobs.dispatch.guards import TERMINAL_RUN_STATUSES

    if status is not None and status in TERMINAL_RUN_STATUSES:
        return False
    log = getattr(child, "log", None) or []
    newest = next((entry for entry in reversed(log) if entry.type is LogEntryType.HANDOFF), None)
    if newest is None:
        return False
    marker = (newest.data or {}).get(MARKER)
    return isinstance(marker, dict) and marker.get("action") in RECOVERING_ACTIONS


def _is_being_walked(manager: TaskManagerLike, child_id: str) -> bool:
    """Whether this child is itself an epic whose own walk is what will land it.

    A child with open children is dispatched as a ``walk`` run, which is terminal the
    moment it is started (task-458): detaching the walk *was* the run. Its ended run is
    therefore evidence of nothing, and reading it as a session that went without
    finishing would ground the outer walk on a nested epic that is working perfectly.
    The signal stays what the docstring above says it is -- the task record -- and this
    is the one case where the run status has to be told to be quiet.
    """
    try:
        children = manager.get_subtasks(child_id)
    except Exception:  # noqa: BLE001 - an unreadable child is judged by its run, as before
        return False
    return any(child.is_open for child in children)


def _poll_child(
    *,
    manager: TaskManagerLike,
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

    if child.ball is not Ball.AGENT and _recovering(child, status):
        # Parked on a login or quota refusal that auth recovery resumes by itself
        # (task-417). Grounding the epic here is what made one expired login stop a whole
        # walk until somebody restarted it (task-417 entry 7). The flight's deadline
        # still bounds the wait.
        if now() >= flight.deadline:
            return verdict(
                ChildVerdict.TIMED_OUT,
                "still parked on a recoverable login or quota refusal when the child's "
                "ceiling ran out",
            )
        return None

    if child.ball is not Ball.AGENT:
        reason = child.ball_reason.value if child.ball_reason else "unstated"
        holder = child.ball.value if child.ball else "nobody"
        return verdict(
            ChildVerdict.PARKED,
            f"ball is {holder}/{reason}: {child.ball_prompt or 'no prompt recorded'}",
        )

    if (
        status is not None
        and status in TERMINAL_RUN_STATUSES
        and not _is_being_walked(manager, child_id)
    ):
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


def utc_stamp(moment: Optional[datetime] = None) -> str:
    """The UTC time a walk's output line is printed at, to the second (task-444).

    Every walk event is written by one of several processes that do not share a clock
    view, and reconstructing the 2026-09-13 double walk meant ordering its lines by lock
    files and run meta because the lines themselves carried no time.
    """
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _resume_parent(
    manager: TaskManagerLike, parent_id: str, *, actor: Optional[str] = None
) -> None:
    """Take a parent off the wait its walk has just lifted (task-467).

    Narrow on purpose: only a parent sitting exactly where a wait put it is moved, so a
    ball a person or another session set in the meantime is never overwritten. It hands
    to ``agent``/``work`` rather than to anybody, because a walk *is* the agent working
    this parent and that is what the record said while the epic was flying.
    """
    from agentjobs.models_v2 import BallReason

    parent = manager.get_task(parent_id)
    if parent is None or not parent.is_open:
        return
    if parent.ball is not Ball.EXTERNAL or parent.ball_reason is not BallReason.DEPENDENCY:
        return
    manager.handoff(
        parent_id,
        actor=actor or "dispatcher",
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt=(
            "The child this epic was waiting on has been resolved, so the walk has "
            "taken off again. Nothing is needed here until it reports."
        ),
    )


def waiting_on_prompt(result: WalkResult, child: Task) -> str:
    """What the parent's ask becomes when its walk is only waiting on a child.

    Addressed to whoever reads the parent next, and deliberately not to a person: the
    ball that carries it is ``external``/``dependency``, so the parent is out of the
    badge and out of the attention episode while the one real click sits on the child.
    """
    landed = ", ".join(result.merged_children) or "none"
    held = "a person" if child.ball is Ball.HUMAN else "an agent"
    return (
        f"Waiting on {child.id}, which {held} is holding. "
        f"{len(result.merged_children)} child/children completed in this walk ({landed}). "
        f"Nothing on this parent needs doing until {child.id} is resolved; when it is, "
        "the walk takes off again on its own."
    )


def already_waiting_on(task: Task, child_id: str) -> bool:
    """Whether *task* already says it is waiting on *child_id*.

    The idempotence the epic walk needs, because a hosted walk re-derives the same wait
    on every poll tick. Without it a parent picked up a progress entry and a handoff
    every few seconds for as long as its child sat in review, which is the log-flood
    version of the same defect: a record nobody can read is a record nobody reads.
    """
    from agentjobs.models_v2 import BallReason

    if task.ball is not Ball.EXTERNAL or task.ball_reason is not BallReason.DEPENDENCY:
        return False
    return child_id in (task.ball_prompt or "")


def walk_handoff_prompt(result: WalkResult) -> str:
    """What the parent's ball prompt becomes when a walk stops for cause.

    The child named is ``stopped_on``, never the last attempt in the list: see that
    field for the incident where deriving it named a child that had merged cleanly.
    """
    who = result.stopped_on or "a child it did not name"
    return (
        f"The epic walk stopped on {who}: {result.detail} "
        f"{len(result.merged_children)} child/children completed in this walk "
        f"({', '.join(result.merged_children) or 'none'}). Read that child's record, "
        "decide what it needs, and restart the walk when it is resolved -- it re-reads "
        "the attempt count off each child's log, so nothing is double-spent."
    )


def describe_settings(
    settings: WalkSettings,
    *,
    posture: Optional[Posture] = None,
    inherited: Optional[Posture] = None,
    inherited_runner: Optional[Tuple[str, Optional[str]]] = None,
) -> Sequence[str]:
    """The bounds, printed before a walk starts so nobody has to guess at them.

    The envelope line is not a bound and is printed anyway, for the reason task-308 gave
    for printing one after every dispatch: "what may these runs do, and who decided
    that" is the question this feature makes expensive to get wrong, and a line that
    appears only when something is unusual trains a reader to skim it. It says
    ``the project default`` when nothing overrides it, which is a claim about what will
    happen rather than an absence.

    The runner line is there for the same reason (task-453). The walk that downgraded
    task-212's children printed the posture they would inherit and nothing about the
    runner, so the only way to see they had landed on the default was to read argv.
    """
    if posture is not None:
        envelope = f"{posture.value} (chosen for this walk)"
    elif inherited is not None:
        envelope = f"{inherited.value} (inherited from the epic's own dispatch)"
    else:
        envelope = "the project default, or each child's own record where it sets one"
    if inherited_runner is not None:
        runner_name, group_name = inherited_runner
        via = f" from group {group_name}" if group_name else ""
        runner_line = (
            f"{runner_name}{via} (inherited from the epic's own dispatch; a child is "
            "refused rather than started elsewhere if it cannot run)"
        )
    else:
        runner_line = "the project's configured runner or group, resolved as each child starts"
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
        f"runner children start on: {runner_line}",
    )


# ----- supervision without a waiting session (task-416, epic-5) -----------------


def supervisor_slot_held(home: Path) -> bool:
    """Whether the process asking is a dispatched run that holds one of the machine's slots.

    Read from the journal, never from the run's own meta: the walk subtracts this slot from
    the ceiling it fills, and a run that could write "I hold nothing" would buy its walk an
    extra child the machine then refuses.

    **This is the exception path now** (task-458). A dispatched epic starts no session at
    all: its dispatch detaches a server-hosted walk and concludes, so nothing is left
    holding a slot and the walk fills the whole ceiling. What still reaches this is a
    person running an attached ``agentjobs dispatch walk`` from inside a session AgentJobs
    dispatched -- rare, and still owed the narrowing, because that session's slot is real.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.dispatch.ledger import calling_run_id
    from agentjobs.execution.errors import ExecutionStoreError

    run_id = calling_run_id()
    if not run_id:
        return False
    try:
        attempt = journal(home).attempt(run_id)
    except ExecutionStoreError:
        return False
    return attempt is not None and attempt.is_live and attempt.takes_slot


def detach_walk(
    *,
    manager: TaskManagerLike,
    project_id: str,
    parent_id: str,
    home: Path,
    settings: WalkSettings,
    posture: Optional[Posture],
    actor: str,
) -> str:
    """Record a walk for the server to advance, and return its id. Starts nothing itself.

    The walk's settings, its posture choice and the actor its outcome is written as are
    saved with it, because the process that will act on them is the server on its next
    tick, not this one. The authorisation is checked now, so a walk nothing authorises is
    refused to the person asking rather than failing silently on a later tick.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.dispatch.ledger import process_alive
    from agentjobs.execution.errors import OwnershipConflict

    parent = manager.get_task(parent_id)
    entry = parent_authorizing_entry(parent) if parent is not None else None
    if parent is None or entry is None:
        raise ParentNotHumanClockedError(
            f"{parent_id} has no entry a dispatch could be caused by, so there is nothing "
            "for a detached walk to run on."
        )
    try:
        walk, _ = journal(home).open_walk(
            project_id=project_id,
            parent_task_id=parent_id,
            authority_entry=entry.id,
            authority_actor=entry.actor,
            settings={
                "max_concurrent": settings.max_concurrent,
                "max_children": settings.max_children,
                "child_timeout_seconds": settings.child_timeout_seconds,
                "posture": posture.value if posture is not None else None,
                "actor": actor,
            },
            host="server",
            holder="server",
            holder_pid=None,
            holder_alive=process_alive,
        )
    except OwnershipConflict as exc:
        raise ParentNotSupervisedError(str(exc)) from exc
    # Deliberately nothing written to the parent here. The walk re-derives each child's
    # authorisation from the parent's newest entry, so an agent's note announcing the
    # detach would shadow the human act it is walking on and every child would be refused.
    # The journal row is the record; the outcome lands on the parent when the walk ends.
    return walk.walk_id


def walk_review_prompt(result: WalkResult) -> str:
    """What the parent's ball prompt becomes when a walk landed every child (task-458).

    The judging half of an epic's supervision, addressed to the person rather than to an
    agent. It says what landed and what the remaining act is, because nothing else on the
    parent does: each child's record says what that child did, and the walk report says
    the order -- neither says that the decision is now somebody's.
    """
    landed = ", ".join(result.merged_children) or "none"
    return (
        f"The epic walk landed every open child ({landed}). Nothing is left running and "
        "nothing holds a slot. What remains is the one judgement the walk deliberately "
        "does not make: read this parent's acceptance criteria against what the children "
        "recorded, and close it where that evidence supports it -- or file what is still "
        "missing as a child and walk it again."
    )


def evaluation_ball_prompt(parent_id: str) -> str:
    """The ask an evaluation run is dispatched against (task-458)."""
    return (
        f"Every open child of {parent_id} landed and the walk is over. Read each child's "
        "record for the evidence it left, judge this parent's acceptance criteria against "
        "it, and close the parent only where that evidence supports it. You hold no "
        "branch and start nothing: if a criterion is not met, say which and hand the "
        "parent back rather than closing it."
    )


def dispatch_parent_evaluation(
    manager: TaskManagerLike,
    parent_id: str,
    *,
    home: Path,
    project: Project,
    project_config: Dict[str, object],
) -> str:
    """Start the one run that judges a landed epic, and return its run id.

    **Attributed to the same human entry the walk itself ran on**, which is the rule the
    walk's own authorisation runs on and the reason this is not an agent authorising a
    dispatch: the person who clicked the epic authorised its ending as much as its
    children. Raises whatever the guards raise -- the caller falls back to asking a human.
    """
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
    from agentjobs.models_v2 import DispatchTrigger

    parent = manager.get_task(parent_id)
    entry = parent_authorizing_entry(parent) if parent is not None else None
    if parent is None or entry is None:
        raise ParentNotHumanClockedError(
            f"{parent_id} has no entry a dispatch could be caused by, so its evaluation "
            "has nothing to run on."
        )
    handle = dispatch_task(
        manager=manager,
        project=project,
        project_config=project_config,
        request=DispatchRequest(
            task_id=parent_id,
            caused_by=entry.id,
            trigger=DispatchTrigger.EVALUATION,
        ),
        home=home,
    )
    return handle.run_id


def record_walk_outcome(
    manager: TaskManagerLike,
    parent_id: str,
    *,
    actor: str,
    result: WalkResult,
    home: Optional[Path] = None,
    project: Optional[Project] = None,
    project_config: Optional[Dict[str, object]] = None,
) -> None:
    """Write how a walk ended onto its parent, and put the parent in front of whoever acts next.

    Shared by the blocking walk and a server-hosted one, so the parent reads the same
    whichever process walked it. It never closes the parent: that stays a judgement about
    the parent's own acceptance criteria -- but since task-458 it no longer leaves that
    judgement to nobody, because there is no longer a supervisor session waiting to make
    it.

    * A walk that is only **waiting** -- a child parked for review, or the one open
      child being somebody else's live work -- hands the parent to
      ``external``/``dependency`` naming that child, and says nothing at all if the
      parent already says so (task-467). A person is not asked twice about one click,
      and a hosted walk re-deriving the same wait every few seconds writes once.
    * A walk that **stopped for cause** hands the parent to ``human/decision`` with the
      child and the reason, exactly as before.
    * A walk that **landed every child** hands the parent to ``human/review`` -- or, at
      posture ``autonomous``, dispatches one evaluation run to do the reading instead,
      which is what keeps an autonomous epic unattended from the click to the close.

    ``home``/``project``/``project_config`` are what an evaluation dispatch needs. Without
    them -- a caller that has no dispatch context, and every test that only wants the
    record written -- the autonomous path degrades to asking a human, which is the
    direction that cannot start a run nobody authorised.
    """
    from agentjobs.models_v2 import BallReason

    if result.stop is WalkStop.ALREADY_SUPERVISED:
        # This walk did nothing, and the one that refused it owns the parent's record. A
        # report here would hand the parent to a human mid-walk (task-444): the refused
        # walk says so to whoever started it, and writes nothing.
        return

    # Decided *before* the report is written, because for a wait the right amount to
    # write is nothing at all: a hosted walk reaches this function on every poll tick
    # for as long as its child sits in review (task-467).
    blocker = waiting_child(manager, parent_id, result.stop, result.stopped_on)
    standing = manager.get_task(parent_id)
    if (
        blocker is not None
        and standing is not None
        and standing.is_open
        and already_waiting_on(standing, blocker.id)
    ):
        return

    manager.add_log_entry(
        parent_id, actor=actor, type=LogEntryType.PROGRESS, body=walk_report(result)
    )
    refreshed = manager.get_task(parent_id)
    if refreshed is None or not refreshed.is_open:
        return
    if not result.stop.is_success:
        if blocker is not None:
            manager.handoff(
                parent_id,
                actor=actor,
                ball=Ball.EXTERNAL,
                ball_reason=BallReason.DEPENDENCY,
                ball_prompt=waiting_on_prompt(result, blocker),
                data={"waiting_on": blocker.id},
            )
            return
        if refreshed.ball is not Ball.HUMAN:
            manager.handoff(
                parent_id,
                actor=actor,
                ball=Ball.HUMAN,
                ball_reason=BallReason.DECISION,
                ball_prompt=walk_handoff_prompt(result),
            )
        return

    prompt = walk_review_prompt(result)
    if _evaluates_itself(refreshed) and home is not None and project is not None:
        # The ball is moved *before* the dispatch, and to the agent, so that a run started
        # here is started against a record that already says what it is for. A dispatch
        # that then fails leaves the parent reading agent/work, which the fallback below
        # corrects; the other order would leave a live evaluation run looking at a
        # ball_prompt written for a person.
        manager.handoff(
            parent_id,
            actor=actor,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt=evaluation_ball_prompt(parent_id),
        )
        try:
            run_id = dispatch_parent_evaluation(
                manager,
                parent_id,
                home=home,
                project=project,
                project_config=project_config or project.load_config(),
            )
        except Exception as exc:  # noqa: BLE001 - any refusal means a person judges it
            manager.add_log_entry(
                parent_id,
                actor=actor,
                type=LogEntryType.PROGRESS,
                body=(
                    "The evaluation run this epic's posture calls for was not started: "
                    f"{type(exc).__name__}: {exc}. The reading is a person's again."
                ),
            )
        else:
            manager.add_log_entry(
                parent_id,
                actor=actor,
                type=LogEntryType.PROGRESS,
                body=(
                    f"Every child landed, so run `{run_id}` was dispatched to judge this "
                    "parent's acceptance criteria against their evidence and close it. "
                    "Posture `autonomous` is what makes that this run's act rather than "
                    "a person's."
                ),
            )
            return
        refreshed = manager.get_task(parent_id)
        if refreshed is None or not refreshed.is_open:
            return

    if refreshed.ball is not Ball.HUMAN:
        manager.handoff(
            parent_id,
            actor=actor,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt=prompt,
        )


def _evaluates_itself(parent: Task) -> bool:
    """Whether this epic was dispatched at a posture that judges its own ending.

    ``autonomous`` and nothing else. Read off the parent's own ``dispatch`` entry, which
    only the manager may write, for the same reason every other inheritance in this module
    is read from there: it is not forgeable over the API.

    **The value, not the inheritance.** ``inherited_posture`` deliberately answers ``None``
    for a posture that came from the project default rather than from a person's click,
    because a default reaches each child on its own and relabelling it would point a reader
    at the wrong record. That distinction is about where a *child's* envelope comes from and
    has nothing to say here. The question this asks is whether this epic's work merges
    without review, and a project whose default is ``autonomous`` answers yes just as
    loudly as a click does.
    """
    entry = parent_dispatch_entry(parent)
    if entry is None or not isinstance(entry.data, dict):
        return False
    try:
        return Posture(entry.data.get("posture")) is Posture.AUTONOMOUS
    except ValueError:
        return False


def _reporter(lines: List[str], walk_id: str) -> Callable[[str], None]:
    return lambda message: lines.append(f"{walk_id}: {message}")


def advance_hosted_walks(
    home: Path,
    *,
    resolve: Callable[[str], Optional[Tuple[TaskManagerLike, Project]]],
    wall: Optional[Callable[[], datetime]] = None,
) -> List[str]:
    """One step of every walk the server hosts. Never raises; returns report lines.

    Each step rebuilds the walk from its record -- the same restore a restarted supervisor
    runs -- so a server restart between two ticks is indistinguishable from the ordinary
    case. A walk whose project cannot be resolved this tick is left for the next.
    """
    from agentjobs.dispatch.journal import journal
    from agentjobs.execution.errors import ExecutionStoreError

    lines: List[str] = []
    try:
        walks = [w for w in journal(home).open_walks() if w.host == "server"]
    except ExecutionStoreError as exc:
        return [f"hosted walks unreadable: {exc}"]
    for walk in walks:
        resolved = resolve(walk.project_id)
        if resolved is None:
            continue
        manager, project = resolved
        saved = walk.settings
        settings = WalkSettings(
            poll_seconds=0.0,
            child_timeout_seconds=float(
                saved.get("child_timeout_seconds") or DEFAULT_CHILD_TIMEOUT_SECONDS
            ),
            max_children=saved.get("max_children"),
            max_concurrent=int(saved.get("max_concurrent") or 1),
        )
        chosen = saved.get("posture")
        try:
            result = _walk_epic(
                manager=manager,
                project=project,
                project_config=project.load_config(),
                parent_id=walk.parent_task_id,
                home=home,
                settings=settings,
                posture=Posture(chosen) if chosen else None,
                on_event=_reporter(lines, walk.walk_id),
                durable=True,
                once=True,
                host="server",
                wall=wall,
                actor=str(saved.get("actor") or "dispatcher"),
            )
        except Exception as exc:  # noqa: BLE001 - reported; the next tick resumes the record
            lines.append(f"{walk.walk_id}: {type(exc).__name__}: {exc}")
            continue
        if result is not None:
            record_walk_outcome(
                manager,
                walk.parent_task_id,
                actor=str(saved.get("actor") or "dispatcher"),
                result=result,
                home=home,
                project=project,
            )
            lines.append(f"{walk.walk_id}: {result.stop.value}")
    return lines
