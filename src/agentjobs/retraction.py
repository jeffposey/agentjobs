"""Withdrawing a demand for attention once the thing it was about is resolved.

**The rule this implements has two clauses and the second one is the new one**
(task-467). A task may demand a person's attention only when there is something for
them to do on that task now -- and the demand must be *withdrawn* when that stops being
true, by something that does not depend on the person noticing.

The first clause is enforced where the demand is written: since task-467 an epic walk
that stops because a child is parked for review hands the parent to
``external``/``dependency`` naming that child, rather than asking the person a second
question about a click they already have. This module is the second clause, and it
exists because the absence of one broke the feature above it.

**What went wrong without it.** On 2026-09-19 task-421's walk grounded on a child
parked for review and handed the parent to ``human``/``decision``. The child was
approved, gated, merged and closed twenty minutes later. Nothing retracted the parent's
ball, because the walk had already written ``state = 'stopped'`` and no supervisor
remained: resolving the child fired nothing at all. The parent stayed in the waiting set
permanently -- and since :mod:`agentjobs.attention` owes exactly one interruptive alert
per episode and an episode only resets when the waiting set *empties*, one stale parent
suppressed the alert for every genuinely new wait that followed it. A stuck row does not
merely add noise here; it silences the alarm.

**Conservative by construction.** This sweep moves a ball a person is holding, which is
the most intrusive write in the system, so it acts only where the record itself says the
demand was about a named child and that child has since let go:

* the task is **open** and holds the ball with a ``human``;
* its ``ball_reason`` is one an epic walk writes -- ``decision`` or ``review``;
* the ask **names at least one of this task's own children**, either through the
  ``waiting_on`` stamp a walk now puts on its handoff or by mentioning the child's id;
* and **every** named child has stopped holding a human ball.

A hand-written ask that names no child is never touched, and neither is one whose child
is still parked. Both are demands a person can still act on.

**It reports before it changes anything.** :func:`survey` is pure and answers with a
finding per task; :func:`retract` applies them. The poller calls the pair each tick and
prints what it did, and ``agentjobs attention repair --dry-run`` is the survey alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Task

__all__ = [
    "CHILD_REFERENCE",
    "RETRACTABLE_REASONS",
    "Finding",
    "retract",
    "survey",
    "waiting_on_stamp",
]

CHILD_REFERENCE = re.compile(r"\btask-\d+\b")
"""A task id mentioned in prose.

The fallback for the records that already exist. Every ask written from task-467 onward
carries a ``waiting_on`` stamp on its handoff entry, which is exact; this reads the
twenty-odd parents written before there was one, and it is safe because a match only
counts when the id belongs to a child of the task being read.
"""

RETRACTABLE_REASONS = (BallReason.DECISION, BallReason.REVIEW)
"""The two reasons an epic walk hands a parent to a person with.

Restricted to these deliberately. ``human``/``answer`` or ``human``/``approval`` is
somebody asking a specific question of a specific person, and no amount of a child
closing makes that question answered.
"""


@dataclass(frozen=True)
class Finding:
    """One task whose demand for attention has outlived its reason.

    ``detail`` is written to be printed on its own in a poller line, because that is
    where most of these will be read and nobody will open a record to interpret one.
    """

    task_id: str
    resolved: Sequence[str]
    """The children the ask named, all of which have stopped holding a human ball."""

    ball: Ball
    ball_reason: BallReason
    prompt: Optional[str]
    detail: str

    def describe(self) -> str:
        """The line a sweep prints for this finding."""
        return f"{self.task_id}: {self.detail}"


def waiting_on_stamp(task: Task) -> Optional[str]:
    """The child the newest handoff on *task* said it was waiting for, if it said.

    Read newest-first and stopped at the first handoff, so a stamp from an earlier wait
    cannot speak for the current one.
    """
    for entry in reversed(task.log):
        if entry.type is not LogEntryType.HANDOFF:
            continue
        data = entry.data or {}
        child = data.get("waiting_on")
        return str(child) if child else None
    return None


def _named_children(task: Task, children: Sequence[Task]) -> List[Task]:
    """This task's own children that its current ask points at.

    The intersection is what makes the prose fallback safe: an id that is not a child of
    this task is somebody referring to other work, and says nothing about whether this
    ask is still live.
    """
    by_id: Dict[str, Task] = {child.id: child for child in children}
    named: Dict[str, Task] = {}
    stamped = waiting_on_stamp(task)
    if stamped and stamped in by_id:
        named[stamped] = by_id[stamped]
    for mentioned in CHILD_REFERENCE.findall(task.ball_prompt or ""):
        if mentioned in by_id:
            named[mentioned] = by_id[mentioned]
    return list(named.values())


def _successor(children: Sequence[Task]) -> Optional[Task]:
    """The open child this task is really waiting on now, if one is identifiable.

    A child still parked on a person comes first -- correcting one stale ask into a
    second one aimed at the same person would be the original defect again -- and an
    agent's live child next.

    **Live means claimed.** A ``ready`` child also holds an agent ball, but nobody is
    working it: it is either claimable, which is work waiting for a walk, or blocked on
    a sibling, which is the same wait one step removed. Naming one parked task-555 on
    2026-09-25 as ``external``/``dependency`` "waiting on task-001" while task-001
    waited only on a ready sibling -- Blocked with nothing blocking it, and no Dispatch
    offered (task-596). Such a parent belongs with ``agent``/``available`` instead.
    """
    for child in children:
        if child.is_open and child.ball is Ball.HUMAN:
            return child
    for child in children:
        if child.ball is Ball.AGENT and child.lifecycle is Lifecycle.ACTIVE:
            return child
    return None


def survey(tasks: Sequence[Task], children_of: Callable[[str], Sequence[Task]]) -> List[Finding]:
    """Every open task whose human ball is about a child that has let go. Changes nothing.

    Pure, so that the dry run and the sweep answer identically and a reader can trust
    one to predict the other.

    ``tasks`` may be the whole corpus or only the human-held part of it: the first
    condition below discards everything else, and both callers narrow it in the store
    because this runs on every poll tick of every project, forever. Children are asked
    for per candidate rather than mapped up front for the same reason -- there are rarely
    more than a handful of candidates, and there are always many more tasks.
    """
    findings: List[Finding] = []
    for task in tasks:
        if not task.is_open or task.ball is not Ball.HUMAN:
            continue
        if task.ball_reason not in RETRACTABLE_REASONS:
            continue
        children = list(children_of(task.id))
        named = _named_children(task, children)
        if not named:
            continue
        holding = [child.id for child in named if child.is_open and child.ball is Ball.HUMAN]
        if holding:
            continue
        successor = _successor(children)
        if successor is not None:
            detail = (
                f"the ask named {', '.join(child.id for child in named)}, which no longer "
                f"holds a person's ball; {successor.id} is what this is waiting on now"
            )
        elif any(child.is_open for child in children):
            detail = (
                f"the ask named {', '.join(child.id for child in named)}, which no longer "
                "holds a person's ball; open children remain, so the epic can be walked "
                "again"
            )
        else:
            detail = (
                f"the ask named {', '.join(child.id for child in named)}, which no longer "
                "holds a person's ball; no open child remains, so what is left is this "
                "task's own judgement"
            )
        findings.append(
            Finding(
                task_id=task.id,
                resolved=[child.id for child in named],
                ball=task.ball,
                ball_reason=task.ball_reason,
                prompt=task.ball_prompt,
                detail=detail,
            )
        )
    return findings


def _correction(
    finding: Finding, children: Sequence[Task]
) -> Optional[tuple[Ball, BallReason, Optional[str], Optional[str]]]:
    """Where *finding*'s task belongs now: ball, reason, prompt, and the child named.

    ``None`` means leave it where it is. That is the answer when no open child remains,
    because the one act the walk deliberately never performs -- judging the parent's own
    acceptance criteria -- is then genuinely available to the person, and a ball on a
    person with something to do is the state this module exists to protect.
    """
    successor = _successor(children)
    resolved = ", ".join(finding.resolved)
    if successor is not None:
        return (
            Ball.EXTERNAL,
            BallReason.DEPENDENCY,
            (
                f"Waiting on {successor.id}. The ask this task was carrying was about "
                f"{resolved}, which has since let go of it, so nothing here needs a "
                f"person until {successor.id} is resolved."
            ),
            successor.id,
        )
    if any(child.is_open for child in children):
        return (
            Ball.AGENT,
            BallReason.AVAILABLE,
            None,
            None,
        )
    return None


def retract(
    tasks: Sequence[Task],
    children_of: Callable[[str], Sequence[Task]],
    *,
    handoff: Callable[..., object],
    log: Callable[..., object],
    actor: str = "dispatcher",
) -> List[str]:
    """Survey, then apply. Returns one line per task, said before it was changed.

    ``handoff`` and ``log`` are the manager's verbs, passed in rather than the manager
    itself so this module depends on the model and nothing else -- and so a caller that
    wants the survey without the writes simply does not call this.

    Never raises for one task's sake: a record that cannot be written is reported and
    the sweep carries on, because the one thing worse than a stale ask is a sweep that
    stops at the first of them.
    """
    lines: List[str] = []
    for finding in survey(tasks, children_of):
        children = list(children_of(finding.task_id))
        correction = _correction(finding, children)
        if correction is None:
            lines.append(f"{finding.task_id}: left with a person -- {finding.detail}")
            continue
        ball, reason, prompt, named = correction
        lines.append(f"{finding.task_id}: {finding.detail}; moved to {ball.value}/{reason.value}")
        try:
            log(
                finding.task_id,
                actor=actor,
                type=LogEntryType.PROGRESS,
                body=(
                    f"This task was holding a person's ball for a reason that has been "
                    f"resolved: {finding.detail}. Nothing was watching for that, so the "
                    f"ask would have stood forever and kept the attention badge lit. It "
                    f"has been moved to `{ball.value}/{reason.value}`. The ask it was "
                    f"carrying was: {finding.prompt or '(none recorded)'}"
                ),
            )
            handoff(
                finding.task_id,
                actor=actor,
                ball=ball,
                ball_reason=reason,
                ball_prompt=prompt,
                data={"waiting_on": named} if named else None,
            )
        except Exception as exc:  # noqa: BLE001 - one bad record must not end the sweep
            lines.append(f"{finding.task_id}: could not be corrected: {type(exc).__name__}: {exc}")
    return lines
