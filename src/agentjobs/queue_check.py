"""The queue-move check: what a reorder just did, said out loud and deterministically.

Implements section 5.4 of ``docs/playbooks-design.md`` and decision P8 (revised).

A human reordering the backlog cannot see the dependency graph, so the commonest
ordering mistake -- promoting something that is blocked -- is invisible at the moment
it is made. Every fact needed to catch it is already in the corpus the move handler
loaded to do the arithmetic, so the check costs one pass over a band and nothing else.

**Deterministic and synchronous. There is no model call in this path, no dispatch, no
debounce and no spend ceiling** -- the whole point of P8's revision is that a check
built out of facts the queue already has can run at the drop instead of two minutes
after it. Nothing here does I/O: the caller hands in a :class:`MoveCheck` assembled
from the corpus it has already read, and gets warnings back.

**The check reports; it never refuses.** The move has already landed by the time these
sentences exist, which is the shape the design argues for at length: a blocking modal
reintroduces exactly the friction a post-hoc notice exists to avoid, and a corrupt band
is something you have to be able to see in order to repair.

**Silence is the normal outcome, and that is a requirement rather than an aspiration.**
A warning that fires on ordinary moves is wallpaper, and wallpaper is worse than
nothing because it trains a reader to click past the one that mattered. Every trigger
below is therefore written to need a specific construction, and every widening of one
should be argued against that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .queue import REPAIR_COMMAND, Placement, QueueProblem

__all__ = [
    "ABOVE_PREREQUISITE",
    "ANCHOR_KEY",
    "KEPT_OVER_KEY",
    "STRONG_ANCHOR",
    "DEMOTED_BLOCKER",
    "NO_OP",
    "PROMOTED_UNCLAIMABLE",
    "QUEUE_BROKEN",
    "WARNING_KINDS",
    "MoveCheck",
    "QueueWarning",
    "apply_placement",
    "check_move",
    "undo_placement",
    "warning_dicts",
]

#: The task that was just promoted is not claimable, so the queue will skip past it
#: where it has been put. The common case, and the existing ``--why`` text pointed at
#: one task instead of at the whole backlog.
PROMOTED_UNCLAIMABLE = "promoted_unclaimable"

#: The order just written cannot execute in the order it reads: the moved task now
#: stands ahead of something it ``needs``, or behind something that needs it.
ABOVE_PREREQUISITE = "above_prerequisite"

#: The move pushed a task down that other open work is waiting on.
DEMOTED_BLOCKER = "demoted_blocker"

#: The band came out in the order it went in, so nothing about the queue changed.
NO_OP = "no_op"

#: The band is not in a state to be reasoned about, so neither is anything above.
QUEUE_BROKEN = "queue_broken"

#: Every kind this module can produce. The closed set a caller may branch on.
WARNING_KINDS: Tuple[str, ...] = (
    QUEUE_BROKEN,
    PROMOTED_UNCLAIMABLE,
    ABOVE_PREREQUISITE,
    DEMOTED_BLOCKER,
    NO_OP,
)


#: The key a ``decision`` entry carries to say that a human read this move's warnings
#: and kept the position anyway. Written by ``TaskManager.keep_queue_move`` and read by
#: ``reorder`` (task-217), which may not move a position anchored this way.
#:
#: **The contract, stated once here so both ends read the same sentence.** The entry is
#: a ``decision``, its ``re`` is the id of the ``queue_move`` entry it answers, and its
#: ``data`` carries ``queue_anchor: "strong"``, the ``band`` and ``queue_position`` the
#: anchor is about, and ``kept_over``: the warnings, verbatim, that were on screen when
#: the human chose. Verbatim rather than recomputed because the anchor's whole claim is
#: about what was shown at that moment, and the queue it was computed from has moved on.
ANCHOR_KEY = "queue_anchor"

#: The one anchor strength this module writes. An *ordinary* anchor is the absence of
#: this entry -- ignoring the notice leaves the move anchored the way any human move is
#: -- so there is deliberately no ``"ordinary"`` value to write.
STRONG_ANCHOR = "strong"

#: Where the kept-over warnings are recorded in that entry's ``data``.
KEPT_OVER_KEY = "kept_over"

#: How many task ids a message names before it says "and N more". The ids are all in
#: ``tasks`` regardless -- this bounds the *sentence*, never the finding, which is why
#: the count of what was left out is stated rather than dropped.
NAMED_LIMIT = 3


@dataclass(frozen=True)
class QueueWarning:
    """One thing worth knowing about a move that has already happened.

    ``kind`` is a member of :data:`WARNING_KINDS` and is what a caller branches on;
    ``message`` is the sentence every surface prints, written once here so the CLI, the
    REST response and the browser cannot drift into three descriptions of one fact.
    ``tasks`` names every task the finding is about, uncapped, even when the message
    names fewer.
    """

    kind: str
    message: str
    tasks: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        """Plain data, for the log entry, the API envelope and the MCP payload."""
        return {"kind": self.kind, "message": self.message, "tasks": list(self.tasks)}


def warning_dicts(warnings: Sequence[QueueWarning]) -> List[Dict[str, object]]:
    """A run of warnings as plain data, in the one shape every surface renders."""
    return [warning.as_dict() for warning in warnings]


@dataclass(frozen=True)
class MoveCheck:
    """Everything the check reads, gathered once by the caller from one corpus read.

    Ids rather than tasks throughout, and orders rather than positions: the check is
    about *sequence*, and a move that exhausts its gap rebalances the band underneath
    itself, so the numbers on either side of one move are not comparable while the
    order always is.
    """

    #: The task the human moved. For a group move this is the root; the children rode
    #: along with it and their relative order is unchanged, so the facts worth stating
    #: are the root's.
    moved: str

    #: The band it moved within. A move never changes a band -- that is reprioritize.
    band: str

    #: The band's open tasks in queue order, before the move and after it.
    before: Tuple[str, ...]
    after: Tuple[str, ...]

    #: Why the moved task is not claimable, or None when it is. Exactly the sentence
    #: ``TaskManager._skip_reason`` produces, so a warning and ``agentjobs next --why``
    #: can never disagree about why the queue would pass it over.
    moved_reason: Optional[str] = None

    #: Open ``needs`` edges among the band's members: task id -> the ids it waits on.
    #: Restricted to this band by the caller, because positions mean nothing across
    #: bands -- a ``high`` task is ahead of a ``medium`` prerequisite whatever anybody
    #: drags, and saying so on every move is the wallpaper this design forbids.
    needs: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    #: How many open tasks are waiting on each id. ``dependency_facts``'s
    #: ``unblocks_count``, computed corpus-wide.
    unblocks: Mapping[str, int] = field(default_factory=dict)

    #: Queue integrity findings for this band and the bands above it.
    problems: Tuple[QueueProblem, ...] = ()


def apply_placement(
    order: Sequence[str],
    movers: Sequence[str],
    placement: Placement,
) -> List[str]:
    """The band's order after ``movers`` are lifted out and re-inserted at ``placement``.

    Structural rather than numeric, deliberately. ``TaskManager._place`` rebalances a
    band whose gap is exhausted, which renumbers every member -- so predicting the new
    order by comparing new numbers against the numbers read before the move is wrong
    exactly when a band is crowded, which is when reordering happens most. The
    placement itself is unambiguous and survives a rebalance untouched.

    A ``before``/``after`` target that is not in ``order`` puts the movers at the
    bottom, matching :func:`~agentjobs.queue.plan_insertion`'s own fallback.
    """
    moving = [item for item in order if item in set(movers)]
    rest = [item for item in order if item not in set(movers)]
    if placement.kind == Placement.TOP:
        index = 0
    elif placement.kind == Placement.BOTTOM:
        index = len(rest)
    elif placement.kind == Placement.BEFORE:
        found = rest.index(str(placement.target)) if placement.target in rest else -1
        index = len(rest) if found < 0 else found
    elif placement.kind == Placement.AFTER:
        found = rest.index(str(placement.target)) if placement.target in rest else -1
        index = len(rest) if found < 0 else found + 1
    else:  # pragma: no cover - placement_from admits no other kind
        raise ValueError(f"unknown placement kind '{placement.kind}'")
    return [*rest[:index], *moving, *rest[index:]]


def undo_placement(order: Sequence[str], moved: str) -> Optional[Placement]:
    """The placement that puts ``moved`` back where this order has it.

    Named as a neighbour rather than as a number, because that is the only thing a
    later move can act on: by the time somebody clicks undo the band may have been
    rebalanced, and the number the task used to hold would put it somewhere nobody
    asked for. ``None`` when the task is not in the order at all.
    """
    if moved not in order:
        return None
    index = list(order).index(moved)
    if index == 0:
        return Placement(Placement.TOP)
    return Placement(Placement.AFTER, order[index - 1])


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _named(ids: Sequence[str]) -> str:
    """The first few ids as a phrase, with the remainder counted rather than dropped."""
    shown = list(ids[:NAMED_LIMIT])
    phrase = ", ".join(shown)
    remaining = len(ids) - len(shown)
    return f"{phrase} and {remaining} more" if remaining else phrase


def check_move(check: MoveCheck) -> List[QueueWarning]:
    """Every warning this move earned, most serious first. Usually none.

    Integrity comes first because it qualifies everything under it: an order with two
    tasks on one number is not an order, so the three consequence warnings below are
    computed against positions nobody should be trusting yet.

    A move that changed no order short-circuits the consequence warnings. It caused
    nothing, so nothing it caused is worth a sentence -- but corruption is still
    reported, because a no-op move is a perfectly ordinary way to discover a band that
    has been broken since long before you touched it.
    """
    warnings: List[QueueWarning] = []

    if check.problems:
        detail = "; ".join(problem.render() for problem in check.problems)
        broken_ids: List[str] = []
        for problem in check.problems:
            broken_ids.extend(problem.task_ids)
        warnings.append(
            QueueWarning(
                kind=QUEUE_BROKEN,
                message=(
                    "The queue is not in a state to be reasoned about, so this move's "
                    f"place in it cannot be trusted: {detail}. Repair it with: "
                    f"{REPAIR_COMMAND}"
                ),
                tasks=tuple(dict.fromkeys(broken_ids)),
            )
        )

    before = list(check.before)
    after = list(check.after)
    if before == after:
        warnings.append(
            QueueWarning(
                kind=NO_OP,
                message=(
                    f"Nothing moved: {check.moved} came out of the '{check.band}' band "
                    "in the place it went in, so the queue will hand out exactly what "
                    "it would have handed out before."
                ),
                tasks=(check.moved,),
            )
        )
        return warnings

    rank_before = before.index(check.moved) if check.moved in before else len(before)
    rank_after = after.index(check.moved) if check.moved in after else len(after)

    if rank_after < rank_before and check.moved_reason is not None:
        warnings.append(
            QueueWarning(
                kind=PROMOTED_UNCLAIMABLE,
                message=(
                    f"{check.moved} cannot be claimed where you have just put it: "
                    f"{check.moved_reason}. The queue will skip past it."
                ),
                tasks=(check.moved,),
            )
        )

    needs = dict(check.needs)
    placed = {task_id: index for index, task_id in enumerate(after)}
    ahead_of = sorted(
        needed
        for needed in needs.get(check.moved, ())
        if needed in placed and placed[needed] > rank_after
    )
    behind = sorted(
        dependent
        for dependent, needed in needs.items()
        if check.moved in needed and dependent in placed and placed[dependent] < rank_after
    )
    if ahead_of or behind:
        clauses = []
        if ahead_of:
            clauses.append(f"ahead of {_named(ahead_of)}, which it needs")
        if behind:
            verb = "which needs it" if len(behind) == 1 else "which need it"
            clauses.append(f"behind {_named(behind)}, {verb}")
        warnings.append(
            QueueWarning(
                kind=ABOVE_PREREQUISITE,
                message=(
                    f"{check.moved} now stands {' and '.join(clauses)}. The order as "
                    "written cannot execute in the order it reads."
                ),
                tasks=tuple(ahead_of + behind),
            )
        )

    unblocks = dict(check.unblocks)
    seated = {task_id: index for index, task_id in enumerate(before)}
    demoted = sorted(
        (
            task_id
            for task_id, index in placed.items()
            if task_id in seated and index > seated[task_id] and unblocks.get(task_id, 0) > 0
        ),
        key=lambda task_id: (-unblocks.get(task_id, 0), task_id),
    )
    if demoted:
        shown = demoted[:NAMED_LIMIT]
        phrase = ", ".join(
            f"{task_id} ({_plural(unblocks[task_id], 'open task')} "
            f"{'needs' if unblocks[task_id] == 1 else 'need'} it)"
            for task_id in shown
        )
        remaining = len(demoted) - len(shown)
        if remaining:
            phrase = f"{phrase}, and {remaining} more"
        warnings.append(
            QueueWarning(
                kind=DEMOTED_BLOCKER,
                message=(
                    f"This pushed {phrase} down the '{check.band}' band. Other open "
                    "work is waiting on them."
                ),
                tasks=tuple(demoted),
            )
        )

    return warnings
