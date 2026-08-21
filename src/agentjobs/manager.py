"""Business logic for managing AgentJobs tasks (schema v2).

The manager owns the state axes. Every change to ``lifecycle``, ``ball``,
``ball_reason``, ``ball_prompt`` or ``outcome`` flows through a verb here --
claim, handoff, release, close -- and every verb appends a log entry, so the
record always shows who moved the ball and why (design doc section 3, rule 5).
Callers never write the axes directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import (
    Any,
    Collection,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
    Union,
    cast,
)

from .operations import (
    Operation,
    OperationConflictError,
    check_revision,
    find_operation,
    replay_or_conflict,
    stamp,
)

from .attachments import AttachmentPayload
from .models_v2 import (
    MANAGER_WRITTEN_LOG_TYPES,
    PRIORITY_RANK,
    Attachment,
    Ball,
    BallReason,
    DeliverableStatus,
    DependencyType,
    DispatchData,
    DispatchSelectionData,
    DispatchMode,
    DispatchOutcome,
    DispatchPosture,
    DispatchResultData,
    DispatchTrigger,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Task,
    utcnow,
)
from .queue import (
    REPAIR_COMMAND,
    QueueAssignment,
    QueueCorruptionError,
    QueuePlace,
    QueueProblem,
    QueueRecord,
    Placement,
    RenumberPass,
    band_entries,
    bands_at_or_above,
    baseline_key,
    find_queue_problems,
    order_key,
    place_of,
    placement_from,
    plan_compaction,
    plan_insertion,
    plan_queue_migration,
    plan_rebalance,
    read_queue_record,
    read_queue_records,
)
from .storage import TaskLoadError, TaskStorage, load_yaml

if TYPE_CHECKING:
    from .webhooks import WebhookManager


@dataclass(frozen=True)
class DependencyFacts:
    """Read-only dependency state for one task."""

    unmet_needs: Tuple[str, ...]
    actionable: bool
    needs_cycles: Tuple[Tuple[str, ...], ...]
    unblocks_count: int
    open_children_count: int


@dataclass(frozen=True)
class SkippedTask:
    """One open task the queue passed over, and the claimability rule that did it."""

    task: str
    queue_position: Optional[int]
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "position": self.queue_position, "reason": self.reason}


@dataclass(frozen=True)
class NextExplanation:
    """Why this task is next -- the answer plus the work it stands in front of.

    ``task`` is None when nothing is claimable, in which case ``skipped`` lists every
    open task with the rule that excluded it. That is the listing a reader wants
    precisely when a tool has just told them there is nothing to do.
    """

    task: Optional[str]
    band: Optional[str]
    queue_position: Optional[int]
    empty_bands_above: Tuple[str, ...]
    skipped: Tuple[SkippedTask, ...]

    def as_dict(self) -> Dict[str, Any]:
        """The structure in design section 9, ready to serialise."""
        return {
            "task": self.task,
            "band": self.band,
            "queue_position": self.queue_position,
            "empty_bands_above": list(self.empty_bands_above),
            "skipped": [item.as_dict() for item in self.skipped],
        }


@dataclass(frozen=True)
class QueueEntry:
    """One task's place in line, with whether it can be taken and why not.

    ``reason`` is exactly the sentence :meth:`TaskManager._skip_reason` produces, so a
    listing and an explanation never disagree about why a task was passed over.
    """

    task: str
    title: str
    queue_position: Optional[int]
    lifecycle: str
    ball: Optional[str]
    claimable: bool
    reason: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "title": self.title,
            "queue_position": self.queue_position,
            "lifecycle": self.lifecycle,
            "ball": self.ball,
            "claimable": self.claimable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class QueueBand:
    """One priority band's open tasks, in the order the queue puts them."""

    band: str
    entries: Tuple[QueueEntry, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {"band": self.band, "entries": [entry.as_dict() for entry in self.entries]}


@dataclass(frozen=True)
class QueueListing:
    """The whole open backlog in queue order, band by band, plus what is broken.

    **Reports rather than raising**, which makes it one of the two deliberate
    exceptions in design section 8: you have to be able to see a broken queue in order
    to fix it. A corrupt band still lists, with ``problems`` naming what is wrong and
    the tasks that lack a position sorted last rather than guessed into a place.
    """

    bands: Tuple[QueueBand, ...]
    problems: Tuple[QueueProblem, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bands": [band.as_dict() for band in self.bands],
            "problems": [
                {
                    "kind": problem.kind,
                    "band": problem.band,
                    "tasks": list(problem.task_ids),
                    "position": problem.position,
                    "message": problem.render(),
                }
                for problem in self.problems
            ],
            "repair_command": REPAIR_COMMAND,
        }


@dataclass(frozen=True)
class _Rejoin:
    """A place computed for a task a generic patch is putting back into a band."""

    position: int
    body: str
    data: Dict[str, Any]


@dataclass(frozen=True)
class QueueRepairReport:
    """What :meth:`TaskManager.repair_queue` did, stated so a human can review it.

    Repair guesses -- it has to, because a duplicate position contains no record of who
    was meant to be first. Everything it guessed is named here, which is what makes the
    guess reviewable instead of silent.
    """

    assigned: Tuple[QueueAssignment, ...] = ()
    rebalanced: Tuple[str, ...] = ()
    unrepairable: Tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.assigned or self.rebalanced)

    def render(self) -> str:
        lines = [
            f"Positions assigned:      {len(self.assigned)}",
            f"Bands rebalanced:        {len(self.rebalanced)}",
            f"Could not be repaired:   {len(self.unrepairable)}",
        ]
        if self.assigned:
            lines += ["", "ASSIGNED (review these -- they were guessed)"]
            lines += [item.render() for item in self.assigned]
        if self.rebalanced:
            lines += ["", "REBALANCED", *(f"  {band}" for band in self.rebalanced)]
        if self.unrepairable:
            lines += ["", "SKIPPED", *(f"  {name}" for name in self.unrepairable)]
        return "\n".join(lines)


WORK_PROMPT = "Execute the spec; log progress and hand off when done."
"""The ask a claim writes for an ordinary task."""

SUPERVISION_PROMPT = (
    "Supervise this epic: start a separate session for one eligible child at a time, "
    "watch its record, and do not work a child yourself. Open now: {children}."
)
"""The ask a claim writes for a task that has open children (task-164).

Stated at the moment of the claim rather than left to a process document, because the
claim is the only point at which the difference is unmissable: an agent that reads
"execute the spec" on an epic has been told, by the tool, to do the thing the tool's own
workflow forbids.
"""

CHILDREN_IN_PROMPT = 8
"""How many child ids the supervision prompt names before it counts the rest."""


def supervision_prompt(children: Sequence[str]) -> str:
    """The supervision ask, naming the open children a claim found."""
    ids = list(children)
    listed = (
        ", ".join(ids)
        if len(ids) <= CHILDREN_IN_PROMPT
        else ", ".join(ids[:CHILDREN_IN_PROMPT]) + f" and {len(ids) - CHILDREN_IN_PROMPT} more"
    )
    return SUPERVISION_PROMPT.format(children=listed)


class TaskNotFoundError(ValueError):
    """The addressed task does not exist.

    A subclass so API routes can map "no such task" to 404 while every other
    ValueError -- a refused claim, an invalid transition -- maps to 409.
    """


class TaskManager:
    """Core task management logic."""

    def __init__(self, storage: TaskStorage, webhook_manager: Optional["WebhookManager"] = None):
        self.storage = storage
        self.webhook_manager = webhook_manager

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _ensure_task_exists(self, task_id: str) -> Task:
        """Retrieve an existing task or raise a descriptive error."""
        task = self.storage.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return task

    def list_tasks(
        self,
        *,
        lifecycle: Optional[Lifecycle] = None,
        ball: Optional[Ball] = None,
        priority: Optional[Priority] = None,
        parent: Optional[str] = None,
    ) -> List[Task]:
        """Return all tasks optionally filtered along the state axes.

        ``ball=Ball.HUMAN`` is the human inbox -- the load-bearing query of the
        v2 design (section 5). ``parent`` narrows to one umbrella's children.
        """
        tasks = self.storage.list_tasks()
        if lifecycle is not None:
            tasks = [task for task in tasks if task.lifecycle == lifecycle]
        if ball is not None:
            tasks = [task for task in tasks if task.ball == ball]
        if priority is not None:
            tasks = [task for task in tasks if task.priority == priority]
        if parent is not None:
            tasks = [task for task in tasks if task.parent == parent]
        return tasks

    def load_errors(self) -> List[TaskLoadError]:
        """Files in the task directory that exist but cannot be read as tasks.

        Exposed so listing surfaces can show them. A broken file that is only logged is
        invisible to someone whose window into the data is a web page.
        """
        return self.storage.load_all().errors

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        return self.storage.load_task(task_id)

    def search_tasks(self, query: str) -> List[Task]:
        """Search tasks by query string."""
        return self.storage.search_tasks(query)

    def _dependency_states(self) -> Dict[str, bool]:
        """Every task id in this project, mapped to whether it is closed.

        Files that exist but cannot be loaded are included as *not* closed. A task whose
        file is unreadable cannot be shown to be finished, and treating it as done would
        let a gate open on the strength of a corrupt file.
        """
        result = self.storage.load_all()
        states = {task.id: task.lifecycle is Lifecycle.CLOSED for task in result.tasks}
        for error in result.errors:
            states.setdefault(error.task_id, False)
        return states

    def _unmet_needs(self, task: Task, states: Dict[str, bool]) -> List[str]:
        """`needs` dependencies that do not permit this task to be claimed, with why.

        A reference to a task that is not in the project blocks, and says so. It
        previously did not: `states.get(id)` returned None for a dangling id, which is
        not False, so a typo'd or renamed dependency silently disabled the gate
        entirely. That is the failure mode D2 exists to prevent -- an edit that quietly
        does nothing -- and it made the codebase strict about a misspelled field while
        permissive about a misspelled task id.

        Blocking rather than validating at save is a narrower fix than
        docs/schema-design.md issue 2 specifies; see the deferral recorded on task-052.
        The choice between them matters less than the property both give you: a
        dependency that cannot be evaluated is never silently treated as satisfied.
        """
        unmet: List[str] = []
        for dep in task.dependencies:
            if dep.type is not DependencyType.NEEDS:
                continue
            if dep.task not in states:
                unmet.append(f"{dep.task} (not a task in this project)")
            elif not states[dep.task]:
                unmet.append(f"{dep.task} (still open)")
        return unmet

    @staticmethod
    def _needs_cycles(tasks: List[Task]) -> Dict[str, Tuple[Tuple[str, ...], ...]]:
        """Return every directed ``needs`` cycle, indexed by each member.

        Missing ids are not vertices: ``_unmet_needs`` already names those. DFS
        records a finite path and never follows an active vertex recursively, so a
        malformed graph cannot make a read recurse forever.
        """

        by_id = {task.id: task for task in tasks}
        edges = {
            task.id: sorted(
                dependency.task
                for dependency in task.dependencies
                if dependency.type is DependencyType.NEEDS and dependency.task in by_id
            )
            for task in tasks
        }
        state: Dict[str, int] = {task_id: 0 for task_id in by_id}
        stack: List[str] = []
        positions: Dict[str, int] = {}
        cycles: set[Tuple[str, ...]] = set()

        def canonical(path: List[str]) -> Tuple[str, ...]:
            members = path[:-1]
            rotations = [tuple(members[index:] + members[:index]) for index in range(len(members))]
            chosen = min(rotations)
            return (*chosen, chosen[0])

        def visit(task_id: str) -> None:
            state[task_id] = 1
            positions[task_id] = len(stack)
            stack.append(task_id)
            for needed_id in edges[task_id]:
                if state[needed_id] == 0:
                    visit(needed_id)
                elif state[needed_id] == 1:
                    cycles.add(canonical(stack[positions[needed_id] :] + [needed_id]))
            stack.pop()
            positions.pop(task_id)
            state[task_id] = 2

        for task_id in sorted(by_id):
            if state[task_id] == 0:
                visit(task_id)

        indexed: Dict[str, List[Tuple[str, ...]]] = {task_id: [] for task_id in by_id}
        for cycle in sorted(cycles):
            for task_id in cycle[:-1]:
                indexed[task_id].append(cycle)
        return {task_id: tuple(task_cycles) for task_id, task_cycles in indexed.items()}

    def dependency_facts(self, tasks: Optional[List[Task]] = None) -> Dict[str, DependencyFacts]:
        """Compute the claim gate, reverse impact, and cycle errors once.

        ``tasks`` selects which ids get an entry in the returned mapping.
        ``open_children_count`` is computed over the whole corpus regardless, exactly
        like the ``actionable`` gate beside it.

        It used to count only within ``tasks``, so a caller passing a filtered subset
        got a number silently relative to that page -- a parent with six open children
        reporting 0, indistinguishable from a parent that has none. Corpus-wide is free
        rather than a trade: ``_open_children`` is already called unconditionally for
        ``actionable``, and inside a request's ``corpus_snapshot`` scope the corpus is
        parsed at most once however many times it is asked for.

        ``unblocks_count`` and ``needs_cycles`` are still derived from ``tasks`` and so
        are still page-relative. Every caller in this repository passes the full corpus
        or nothing, so neither is wrong today; both are the same trap and neither is in
        this task's scope. See task-180.
        """

        project_tasks = tasks if tasks is not None else self.storage.list_tasks()
        states = self._dependency_states()
        open_children = self._open_children()
        cycles = self._needs_cycles(project_tasks)
        reverse_open_needs: Dict[str, int] = {}
        for task in project_tasks:
            if not task.is_open:
                continue
            for dependency in task.dependencies:
                if dependency.type is DependencyType.NEEDS:
                    reverse_open_needs[dependency.task] = (
                        reverse_open_needs.get(dependency.task, 0) + 1
                    )

        facts: Dict[str, DependencyFacts] = {}
        for task in project_tasks:
            unmet_needs = tuple(self._unmet_needs(task, states))
            facts[task.id] = DependencyFacts(
                unmet_needs=unmet_needs,
                actionable=(
                    task.lifecycle is Lifecycle.READY
                    and not unmet_needs
                    and task.id not in open_children
                ),
                needs_cycles=cycles.get(task.id, ()),
                unblocks_count=reverse_open_needs.get(task.id, 0),
                open_children_count=len(open_children.get(task.id, ())),
            )
        return facts

    def get_subtasks(self, task_id: str) -> List[Task]:
        """The tasks whose ``parent`` is this one, ordered by id.

        Raises TaskNotFoundError for an id that is not a task. "No children" and "no
        such task" are different answers and an empty list conflates them -- which
        matters most for the caller most likely to get the id wrong, a URL.
        """
        self._ensure_task_exists(task_id)
        children = [task for task in self.storage.list_tasks() if task.parent == task_id]
        children.sort(key=lambda task: task.id)
        return children

    def _open_children(self) -> Dict[str, List[str]]:
        """Parent task id -> ids of its children that are still open, sorted.

        A parent absent from this mapping has no open children and is therefore
        claimable like any other task. Files that fail to load are not counted: their
        `parent` cannot be read, so they are invisible as children of anything. They
        still show up in the broken-files listing, which is where an unreadable file
        gets dealt with.
        """
        open_children: Dict[str, List[str]] = {}
        for task in self.storage.list_tasks():
            if task.parent and task.is_open:
                open_children.setdefault(task.parent, []).append(task.id)
        for ids in open_children.values():
            ids.sort()
        return open_children

    def _skip_reason(
        self,
        task: Task,
        priority: Optional[Priority],
        agent: Optional[str],
        states: Dict[str, bool],
        open_children: Dict[str, List[str]],
    ) -> Optional[str]:
        """Why this task is not claimable right now, or None if it is.

        One string per rule, in a fixed order, because a task usually breaks more than
        one and "active *and* has four open children" answers a question nobody asked.
        Lifecycle first: it is the reason a reader can act on.
        """
        if task.lifecycle is not Lifecycle.READY:
            holder = task.ball.value if task.ball else "nobody"
            return f"not ready ({task.lifecycle.value}, held by {holder})"
        children = open_children.get(task.id)
        if children:
            return f"has {len(children)} open " + ("child" if len(children) == 1 else "children")
        unmet = self._unmet_needs(task, states)
        if unmet:
            return f"waiting on {', '.join(unmet)}"
        eligible = task.assignment.eligible
        if agent is not None and eligible and agent not in eligible:
            return f"restricted to {', '.join(eligible)}"
        if priority is not None and task.priority is not priority:
            return f"outside the requested '{priority.value}' band"
        return None

    def _claimable(
        self,
        tasks: Sequence[Task],
        priority: Optional[Priority],
        agent: Optional[str],
        states: Dict[str, bool],
        open_children: Dict[str, List[str]],
    ) -> List[Task]:
        """The claimability filter, unchanged by the queue. It decides *whether*."""
        return [
            task
            for task in tasks
            if self._skip_reason(task, priority, agent, states, open_children) is None
        ]

    def get_next_task(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
    ) -> Optional[Task]:
        """The claimable task that stands first in line: ``(band, queue_position)``.

        Claimability decides *whether* -- ready, eligible for the asker, no unmet needs,
        no open children -- and the queue decides *which*. A blocked task does not block
        the queue: it is filtered out and the queue moves past it.

        **No timestamp participates, including as a fallback.** Until task-081 this
        sorted by ``updated`` descending, which meant that logging progress on a task
        promoted it, and that the answer to "what should I work on" changed because
        somebody wrote a note. If the queue cannot be trusted the answer is
        :class:`QueueCorruptionError`, not a guess from a different field -- see
        :meth:`assert_queue_integrity`.
        """
        tasks = self.storage.list_tasks()
        candidates = self._claimable(
            tasks, priority, agent, self._dependency_states(), self._open_children()
        )
        if not candidates:
            return None
        winning_rank = min(task.priority_rank() for task in candidates)
        self.assert_queue_integrity(bands_at_or_above(winning_rank))
        candidates.sort(key=order_key)
        return candidates[0]

    def explain_next(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
    ) -> "NextExplanation":
        """The winner, and every open task ahead of it with the rule that excluded it.

        The scheduler now has a defensible answer, so it gives it. This is the reply to
        the question a human actually asks when a tool hands them a task -- "why not the
        one I was expecting?" -- and it makes the queue self-teaching: somebody who sees
        their favourite task skipped for "has 7 open children" has just learned a rule.

        ``skipped`` covers only tasks ahead of the winner, which is also why the
        integrity check reaches past the claimable candidates: the explanation asserts
        an order over those tasks, so their positions have to be trustworthy too.
        """
        tasks = self.storage.list_tasks()
        states = self._dependency_states()
        open_children = self._open_children()
        candidates = self._claimable(tasks, priority, agent, states, open_children)

        winner: Optional[Task] = None
        if candidates:
            winning_rank = min(task.priority_rank() for task in candidates)
            self.assert_queue_integrity(bands_at_or_above(winning_rank))
            winner = min(candidates, key=order_key)

        # With no winner nothing is ahead of anything, so every open task is skipped --
        # which is the listing a reader wants when a tool has just told them there is
        # nothing to do.
        limit = order_key(winner) if winner is not None else None
        ahead = sorted(
            (task for task in tasks if task.is_open and (limit is None or order_key(task) < limit)),
            key=order_key,
        )
        skipped = tuple(
            SkippedTask(
                task=task.id,
                queue_position=task.queue_position,
                reason=reason,
            )
            for task in ahead
            for reason in [self._skip_reason(task, priority, agent, states, open_children)]
            if reason is not None
        )

        empty_above: Tuple[str, ...] = ()
        if winner is not None:
            occupied = {task.priority.value for task in tasks if task.is_open}
            empty_above = tuple(
                band.value
                for band in sorted(PRIORITY_RANK, key=lambda item: PRIORITY_RANK[item])
                if PRIORITY_RANK[band] < winner.priority_rank() and band.value not in occupied
            )

        return NextExplanation(
            task=winner.id if winner else None,
            band=winner.priority.value if winner else None,
            queue_position=winner.queue_position if winner else None,
            empty_bands_above=empty_above,
            skipped=skipped,
        )

    def queue_listing(
        self,
        *,
        agent: Optional[str] = None,
    ) -> "QueueListing":
        """The whole open backlog in queue order, band by band. This is the review copy.

        Every band is listed, including the empty ones, because "critical is empty" is
        a fact a reader of an ordered backlog wants stated rather than inferred from a
        missing heading. Every open task appears with the claimability rule that would
        exclude it, from the same ``_skip_reason`` selection uses, so the list and the
        explanation can never disagree.

        **It reports rather than raising** -- design section 8's other deliberate
        exception, alongside ``check_queue``. A task with no position sorts last in its
        band instead of being guessed into a place, and ``problems`` says what is wrong
        with the corpus. A listing that refused to render a broken queue would be a
        listing you could not use to fix one.
        """
        tasks = self.storage.list_tasks()
        states = self._dependency_states()
        open_children = self._open_children()

        by_band: Dict[str, List[Task]] = {band.value: [] for band in PRIORITY_RANK}
        for task in tasks:
            if task.is_open:
                by_band.setdefault(task.priority.value, []).append(task)

        bands: List[QueueBand] = []
        for band in sorted(PRIORITY_RANK, key=lambda item: PRIORITY_RANK[item]):
            members = by_band.get(band.value, [])
            # None last, and by id within a tie, so a corrupt band still renders in a
            # stable order rather than one that changes between two identical runs.
            members.sort(
                key=lambda task: (task.queue_position is None, task.queue_position or 0, task.id)
            )
            entries = []
            for task in members:
                reason = self._skip_reason(task, None, agent, states, open_children)
                entries.append(
                    QueueEntry(
                        task=task.id,
                        title=task.title,
                        queue_position=task.queue_position,
                        lifecycle=task.lifecycle.value,
                        ball=task.ball.value if task.ball else None,
                        claimable=reason is None,
                        reason=reason,
                    )
                )
            bands.append(QueueBand(band=band.value, entries=tuple(entries)))

        return QueueListing(bands=tuple(bands), problems=tuple(self.check_queue()))

    # ------------------------------------------------------------------
    # Creation and generic edits
    # ------------------------------------------------------------------

    def _validate_parent(self, task_id: str, parent: Optional[str]) -> None:
        """Refuse a parent that does not exist, is the task itself, or closes a cycle.

        The model already rejects self-parenting, because that check needs nothing but
        the task. The other two need the whole store, which is why they live here: a
        dangling `parent` is the same failure `_unmet_needs` guards against on
        dependencies -- an id that looks meaningful, points at nothing, and quietly
        disables every behaviour keyed on it.
        """
        if parent is None:
            return
        if parent == task_id:
            raise ValueError(f"Task '{task_id}' cannot be its own parent.")

        parents = {task.id: task.parent for task in self.storage.list_tasks()}
        if parent not in parents:
            raise ValueError(f"Parent task '{parent}' does not exist.")

        # Walk up from the proposed parent. Meeting this task means the edit would
        # close a loop; meeting anything twice means a loop is already up there and is
        # not this edit's doing, so stop rather than spin.
        ancestry = [parent]
        seen = {parent}
        cursor = parents[parent]
        while cursor is not None:
            if cursor == task_id:
                chain = " -> ".join([task_id, *reversed(ancestry)])
                raise ValueError(
                    f"Task '{task_id}' cannot be parented to '{parent}': that would "
                    f"create a cycle ({chain})."
                )
            if cursor in seen:
                break
            seen.add(cursor)
            ancestry.append(cursor)
            cursor = parents.get(cursor)

    def create_task(
        self,
        *,
        id: Optional[str] = None,
        title: str,
        description: str,
        summary: Optional[str] = None,
        priority: Priority = Priority.MEDIUM,
        category: str = "general",
        lifecycle: Lifecycle = Lifecycle.DRAFT,
        actor: Optional[str] = None,
        operation_id: Optional[str] = None,
        attachments: Optional[Sequence[AttachmentPayload]] = None,
        placement: Optional[Placement] = None,
        **kwargs: Any,
    ) -> Task:
        """Create a new task, generating an identifier when omitted.

        Tasks are born ``draft`` (ball: human/spec) or ``ready`` (ball:
        agent/available). Any other starting state would skip the transitions the log
        exists to record.

        With an ``operation_id``, creation is idempotent: a retry finds the task the
        first attempt made instead of producing a second one.

        **Both project locks are held, in the fixed order** ``.creation`` then
        ``.queue`` (design section 7). Creation decides two things by computing a
        maximum and then writing -- the next id and the bottom of the band -- and those
        are two separate races. The creation lock alone left the second one open: a
        create racing a *move* can duplicate a position even when it races no other
        create.

        ``placement`` is for the caller who already knows where the task goes. The
        default is the bottom of its band: a task nobody has placed does not get to
        preempt an order somebody thought about.
        """
        operation = (
            None
            if operation_id is None
            else Operation(
                id=operation_id,
                kind="create",
                actor=actor or "system",
                payload={"id": id, "title": title, "lifecycle": Lifecycle(lifecycle).value},
            )
        )
        with self.storage.creation_lock():
            if operation is not None:
                existing = self._find_created_by(operation)
                if existing is not None:
                    return existing
            with self.storage.queue_lock():
                return self._create_unlocked(
                    id=id,
                    title=title,
                    description=description,
                    summary=summary,
                    priority=priority,
                    category=category,
                    lifecycle=lifecycle,
                    actor=actor,
                    operation=operation,
                    attachments=attachments,
                    placement=placement,
                    **kwargs,
                )

    def _find_created_by(self, operation: Operation) -> Optional[Task]:
        """Return the task a previous attempt at this creation produced, if any.

        A linear scan of the corpus. Deliberate: the alternative is an index file,
        which is a second thing that can disagree with the tasks it indexes, and this
        runs only when a caller supplies an operation_id -- creation, not the hot path.
        A project large enough for the scan to matter has outgrown YAML storage for
        other reasons first.
        """
        for task in self.storage.list_tasks():
            marker = find_operation(task, operation.id)
            if marker is None:
                continue
            if marker.get("fingerprint") != operation.fingerprint:
                raise OperationConflictError(
                    f"Operation id {operation.id!r} already created task {task.id!r} "
                    "with different arguments. Nothing was written. Use a new "
                    "operation_id, or resend the original request exactly."
                )
            return task
        return None

    def _create_unlocked(
        self,
        *,
        id: Optional[str],
        title: str,
        description: str,
        summary: Optional[str],
        priority: Priority,
        category: str,
        lifecycle: Lifecycle,
        actor: Optional[str],
        operation: Optional[Operation],
        attachments: Optional[Sequence[AttachmentPayload]] = None,
        placement: Optional[Placement] = None,
        **kwargs: Any,
    ) -> Task:
        """Build and persist one task. The caller holds both project locks."""
        task_id = id or self.storage.generate_task_id()
        if self.storage.load_task(task_id):
            raise ValueError(f"Task '{task_id}' already exists.")
        parent = kwargs.get("parent")
        self._validate_parent(task_id, parent if isinstance(parent, str) else None)

        lifecycle = Lifecycle(lifecycle)
        if lifecycle not in (Lifecycle.DRAFT, Lifecycle.READY):
            raise ValueError(
                f"A task cannot be created '{lifecycle.value}'; it starts draft or ready."
            )

        spec_payload = dict(kwargs.pop("spec", None) or {})
        spec_payload.setdefault("description", description)
        spec_payload.setdefault("summary", summary or title)

        now = utcnow()
        task_kwargs: Dict[str, object] = {
            "id": task_id,
            "title": title,
            "created": now,
            "updated": now,
            "lifecycle": lifecycle,
            "priority": priority,
            "category": category,
            "spec": spec_payload,
        }
        if lifecycle is Lifecycle.DRAFT:
            task_kwargs.update(
                ball=Ball.HUMAN,
                ball_reason=BallReason.SPEC,
                ball_prompt=kwargs.pop("ball_prompt", None) or "Finish specifying this task.",
            )
        else:
            # agent/available: the spec is itself the ask, no prompt required.
            task_kwargs.update(ball=Ball.AGENT, ball_reason=BallReason.AVAILABLE)
            kwargs.pop("ball_prompt", None)
        task_kwargs.update(kwargs)
        # A task is born open, and rule 6 says an open task has a place in line, so
        # creation has to give it one or produce a model that will not validate
        # (design section 5.1). The queue lock is held by create_task above, so the
        # band read below cannot be overtaken between the read and the write.
        chosen = placement or Placement(Placement.BOTTOM)
        if chosen.target is not None:
            self._require_band_member(chosen.target, Priority(priority))
        if "queue_position" not in task_kwargs:
            task_kwargs["queue_position"] = self._place(Priority(priority), chosen)[0]

        task = Task.model_validate(task_kwargs)
        creator = actor or (operation.actor if operation is not None else None)
        # An attachment has to hang off an entry, and on a fresh task the creation
        # entry is the only one there is -- so supplying images is itself a reason to
        # write it, even when nobody named a creator.
        stored = self._store_attachments(task_id, attachments)
        if creator is None and stored:
            creator = "system"
        if creator is not None:
            # The creation entry carries two things: the operation marker a retry
            # finds, and who created the task. Either one on its own is reason enough
            # to write it -- a caller that names an actor and gets no attribution has
            # been silently ignored, which is the failure actors.py exists to prevent.
            # A create that names neither still starts with an empty log, as before.
            self._append_entry(
                task,
                actor=creator,
                type=LogEntryType.TRANSITION,
                body=f"Created {lifecycle.value} by {creator}.",
                data={"lifecycle": lifecycle.value},
                operation=operation,
                attachments=stored,
            )
        if placement is not None:
            # Somebody decided where this goes, so the record says so. The default
            # bottom-of-band is not a decision and writes nothing: an entry on every
            # create saying "it went last" would bury the ones that mean something.
            self._append_entry(
                task,
                actor=creator or "system",
                type=LogEntryType.QUEUE_MOVE,
                body=f"Created at {placement.describe()} of the {priority.value} band.",
                data={
                    "band": priority.value,
                    "from": None,
                    "to": task.queue_position,
                    "placement": placement.as_data(),
                },
            )
        return self.storage.save_task(task)

    def update_task(
        self,
        task_id: str,
        *,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
        actor: Optional[str] = None,
        **updates: object,
    ) -> Task:
        """Apply a partial update to a task.

        With an ``expected_revision`` the update is refused if the task moved since the
        caller read it, so an edit computed from stale content cannot silently discard
        someone else's. Without one, behaviour is exactly as before -- last write wins
        -- which keeps existing callers working.

        **A patch that changes a task's band, or reopens it, is intercepted** and given
        a place in the band it is joining, under the queue lock, before the patch is
        applied (design sections 5.3 and 5.4). This is not a courtesy: ``priority`` is
        an ordinary content field on every existing caller, and a band change that
        carried its old number across would put two tasks on one position with nothing
        in the record to say which came first. The allowlist never gains
        ``queue_position``, so the number itself stays unreachable by patch.
        """
        operation = self._operation(
            operation_id,
            "update_content",
            actor or "system",
            {"updates": {key: value for key, value in sorted(updates.items())}},
        )
        rejoin = self._rejoining_the_queue(task_id, updates)

        def apply(existing: Task) -> Optional[Task]:
            if replay_or_conflict(existing, operation):
                return None
            check_revision(existing, expected_revision)
            if "parent" in updates:
                parent = updates["parent"]
                self._validate_parent(existing.id, parent if isinstance(parent, str) else None)
            payload = existing.model_dump(mode="python", by_alias=True, exclude={"display_status"})
            payload.update(updates)
            payload["id"] = existing.id
            payload["created"] = existing.created
            if rejoin is not None:
                payload["queue_position"] = rejoin.position
            updated = Task.model_validate(payload)
            if rejoin is not None:
                self._append_entry(
                    updated,
                    actor=actor or "system",
                    type=LogEntryType.QUEUE_MOVE,
                    body=rejoin.body,
                    data=rejoin.data,
                )
            if operation is not None:
                # A content update writes no log entry of its own, so without this
                # there would be nowhere to record the operation and a retry could not
                # recognise itself. Appended only when an operation_id was supplied,
                # so ordinary edits do not start filling the log with churn.
                self._append_entry(
                    updated,
                    actor=operation.actor,
                    type=LogEntryType.NOTE,
                    body=f"Updated {', '.join(sorted(updates))}.",
                    data={"fields": sorted(updates)},
                    operation=operation,
                )
            return updated

        return self._mutate(task_id, apply)

    def _rejoining_the_queue(
        self, task_id: str, updates: Mapping[str, object]
    ) -> Optional["_Rejoin"]:
        """The place a generic patch has to be given, computed under the queue lock.

        Two patches put a task into a band it is not currently in line in:

        * a **band change** on an open task (design section 5.3), and
        * a **reopen** -- a closed task whose ``lifecycle`` is patched back to something
          open (design section 5.4), which re-enters at the bottom and does not remember
          where it used to be. The queue moved on without it.

        There is deliberately no ``reopen`` verb yet, so this generic patch is the only
        path that can produce one, and it is the path that has to hold the lock.

        Returns None -- doing no work and taking no lock -- for the overwhelming
        majority of patches, which touch neither field.
        """
        if "priority" not in updates and "lifecycle" not in updates:
            return None
        existing = self.get_task(task_id)
        if existing is None:
            return None

        band = (
            Priority(cast(Any, updates["priority"])) if "priority" in updates else existing.priority
        )
        reopening = not existing.is_open and (
            "lifecycle" in updates
            and Lifecycle(cast(Any, updates["lifecycle"])) is not Lifecycle.CLOSED
        )
        rebanding = existing.is_open and band is not existing.priority
        if not (reopening or rebanding):
            return None

        with self.storage.queue_lock():
            position = self._place(band, Placement(Placement.BOTTOM), excluding=(task_id,))[0]
        reason = "Reopened" if reopening else f"Moved from the {existing.priority.value} band"
        data: Dict[str, Any] = {
            "band": band.value,
            "from": existing.queue_position,
            "to": position,
            "placement": Placement(Placement.BOTTOM).as_data(),
        }
        if rebanding:
            data["from_band"] = existing.priority.value
        return _Rejoin(
            position=position,
            body=f"{reason} into the bottom of the {band.value} band.",
            data=data,
        )

    def delete_task(self, task_id: str) -> bool:
        """Delete task from storage."""
        return self.storage.delete_task(task_id)

    def mark_deliverable_complete(
        self,
        task_id: str,
        deliverable_path: str,
    ) -> Task:
        """Mark deliverable as done."""

        def apply(task: Task) -> Task:
            for deliverable in task.deliverables:
                if deliverable.path == deliverable_path:
                    deliverable.status = DeliverableStatus.DONE
                    break
            else:
                raise ValueError(
                    f"Deliverable '{deliverable_path}' not found for task '{task_id}'."
                )
            return task

        return self._mutate(task_id, apply)

    # ------------------------------------------------------------------
    # The state verbs (design doc section 5)
    #
    # Preconditions are checked *inside* the lock against a fresh read via
    # storage.mutate_task -- checking beforehand is exactly the double-claim race
    # task-055 closed. Two agents racing claim_task produce one winner and one
    # ValueError, never two owners.
    # ------------------------------------------------------------------

    def _mutate(self, task_id: str, mutator: Any) -> Task:
        """mutate_task, with a missing task reported as TaskNotFoundError."""
        if not self.storage._task_path(task_id).exists():
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return self.storage.mutate_task(task_id, mutator)

    def _store_attachments(
        self, task_id: str, payloads: Optional[Sequence[AttachmentPayload]]
    ) -> Optional[List[Attachment]]:
        """Write each payload beside the task and return the records referencing them.

        Kept in the manager rather than at the API boundary so the blob and the entry
        that points at it are written by one call. An AttachmentError raised here
        aborts the whole verb, which is what stops a task from recording feedback that
        cites an image nobody managed to store.
        """
        if not payloads:
            return None
        return [self.storage.attachments.write(task_id, payload) for payload in payloads]

    @staticmethod
    def _append_entry(
        task: Task,
        *,
        actor: str,
        type: LogEntryType,
        body: Optional[str] = None,
        re: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
        operation: Optional[Operation] = None,
        attachments: Optional[List[Attachment]] = None,
    ) -> LogEntry:
        """Append one entry, stamping the operation that produced it.

        The marker rides in the entry rather than in a side table, so replay detection
        is as durable as the task itself and survives a restart of every process
        involved -- which is precisely when a client retries.
        """
        entry = LogEntry(
            id=task.next_log_id(),
            ts=utcnow(),
            actor=actor,
            type=type,
            body=body,
            re=re,
            data=stamp(data, operation),
            # None rather than [] when there are none, so an entry without images does
            # not carry an empty key -- every existing task file would otherwise gain a
            # line on its next write, for a field it does not use.
            attachments=attachments or None,
        )
        task.log.append(entry)
        return entry

    def _operation(
        self,
        operation_id: Optional[str],
        kind: str,
        actor: str,
        payload: Dict[str, Any],
    ) -> Optional[Operation]:
        """Build the operation record for a mutation, when the caller supplied an id."""
        if operation_id is None:
            return None
        return Operation(id=operation_id, kind=kind, actor=actor, payload=payload)

    def claim_task(
        self,
        task_id: str,
        *,
        agent: str,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Take ownership of a ready task, or refuse because someone else already did.

        **A task with open children can be claimed, and what it hands you is the
        supervisor's seat** (task-164). It used to be refused outright, on the reasoning
        that "an umbrella is finished by its children, so there is no work to take here".
        That reasoning stopped being true when driving an epic became a described job:
        picking the eligible child, starting a session for it, watching the record, and
        judging the parent's own acceptance criteria at the end is work, it belongs to
        one agent for the length of the epic, and refusing the claim left the only agent
        doing it with no way to say so on the record.

        What survives the change is the distinction between *naming* a task and *being
        handed* one. ``get_next_task`` still skips umbrellas, so nothing is ever given an
        epic by asking what is next; only a caller that named this id gets one. The
        ``ball_prompt`` written here says which seat was taken, so an agent that claimed
        an epic without meaning to is told at the moment it claims rather than four
        commits into working a child itself.
        """
        states = self._dependency_states()
        open_children = self._open_children()
        operation = self._operation(operation_id, "claim", agent, {})

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            if task.lifecycle is not Lifecycle.READY:
                owner = task.assignment.owner
                raise ValueError(
                    f"Task '{task_id}' is not available to claim "
                    f"(it is {task.display_status.lower()}"
                    + (f", owned by {owner}" if owner else "")
                    + ")"
                )
            if task.assignment.eligible and agent not in task.assignment.eligible:
                eligible = ", ".join(task.assignment.eligible)
                raise ValueError(
                    f"Task '{task_id}' is claimable only by: {eligible} (not '{agent}')"
                )
            unmet = self._unmet_needs(task, states)
            if unmet:
                raise ValueError(f"Task '{task_id}' has unmet dependencies: {', '.join(unmet)}")
            children = open_children.get(task_id)
            task.lifecycle = Lifecycle.ACTIVE
            task.assignment.owner = agent
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.WORK
            task.ball_prompt = supervision_prompt(children) if children else WORK_PROMPT
            self._append_entry(
                task,
                actor=agent,
                type=LogEntryType.TRANSITION,
                body=(
                    f"Claimed by {agent} to supervise {len(children)} open sub-task(s)."
                    if children
                    else f"Claimed by {agent}."
                ),
                data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    def handoff(
        self,
        task_id: str,
        *,
        actor: str,
        ball: Ball,
        ball_reason: BallReason,
        ball_prompt: Optional[str] = None,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
        attachments: Optional[Sequence[AttachmentPayload]] = None,
    ) -> Task:
        """Move the ball. The ask travels with it, by schema requirement.

        ``attachments`` are images evidencing this handoff -- a screenshot of the thing
        being objected to. They are written inside the mutation, so a stored file
        without an entry referencing it is not a state this verb can produce.
        """
        operation = self._operation(
            operation_id,
            "handoff",
            actor,
            {
                "ball": Ball(ball).value,
                "ball_reason": BallReason(ball_reason).value,
                "ball_prompt": ball_prompt,
                "body": body,
            },
        )

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            check_revision(task, expected_revision)
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is closed; the ball cannot move.")
            task.ball = Ball(ball)
            task.ball_reason = BallReason(ball_reason)
            task.ball_prompt = ball_prompt
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.HANDOFF,
                body=body or ball_prompt,
                data={"ball": task.ball.value, "ball_reason": task.ball_reason.value},
                operation=operation,
                attachments=self._store_attachments(task.id, attachments),
            )
            return task

        task = self._mutate(task_id, apply)
        self._fire(
            "task.handoff",
            task,
            {
                "triggered_by": actor,
                "ball": task.ball.value if task.ball else None,
                "ball_reason": task.ball_reason.value if task.ball_reason else None,
                "ball_prompt": task.ball_prompt,
            },
        )
        return task

    def release_task(
        self,
        task_id: str,
        *,
        actor: str,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Bow out cleanly: active goes back to ready, unclaimed and available."""
        operation = self._operation(operation_id, "release", actor, {"body": body})

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            if task.lifecycle is not Lifecycle.ACTIVE:
                raise ValueError(
                    f"Task '{task_id}' is not active (it is {task.display_status.lower()}); "
                    "only claimed work can be released."
                )
            task.lifecycle = Lifecycle.READY
            task.assignment.owner = None
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.AVAILABLE
            task.ball_prompt = None
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.TRANSITION,
                body=body or f"Released by {actor}; back in the pool.",
                data={"lifecycle": "ready", "ball": "agent", "ball_reason": "available"},
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    def promote_task(
        self,
        task_id: str,
        *,
        actor: str,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
    ) -> Task:
        """Declare a draft's spec finished: draft becomes ready, unclaimed and available.

        This is the only exit from ``draft``. ``handoff`` moves the ball and deliberately
        leaves the lifecycle alone, so without this verb a drafted task stays unclaimable
        for good.

        Completeness is not checked here. What counts as a finished spec is
        ``agentjobs validate``'s question, and duplicating a weaker version of it in the
        verb would refuse records their author considers ready.
        """
        operation = self._operation(operation_id, "promote", actor, {"body": body})

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            check_revision(task, expected_revision)
            if task.lifecycle is not Lifecycle.DRAFT:
                raise ValueError(
                    f"Task '{task_id}' is not a draft (it is {task.display_status.lower()}); "
                    "only a draft can be promoted."
                )
            task.lifecycle = Lifecycle.READY
            task.assignment.owner = None
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.AVAILABLE
            task.ball_prompt = None
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.TRANSITION,
                body=body or f"Promoted by {actor}; the spec is finished and it is claimable.",
                data={"lifecycle": "ready", "ball": "agent", "ball_reason": "available"},
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    def close_task(
        self,
        task_id: str,
        *,
        actor: str,
        outcome: Outcome,
        body: Optional[str] = None,
        archive: bool = False,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
    ) -> Task:
        """End the task with an outcome. How it ended is data, not a lifecycle fork."""
        operation = self._operation(
            operation_id,
            "close",
            actor,
            {"outcome": Outcome(outcome).value, "body": body, "archive": archive},
        )

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            check_revision(task, expected_revision)
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is already closed.")
            task.lifecycle = Lifecycle.CLOSED
            task.outcome = Outcome(outcome)
            task.ball = None
            task.ball_reason = None
            task.ball_prompt = None
            task.assignment.owner = None
            # Leaving the line, in the same write that drops the ball (design section
            # 5.4). No queue lock is needed even once task-205 adds one: removing a
            # value cannot create a duplicate, which keeps this hot path cheap.
            task.queue_position = None
            if archive:
                task.archived = True
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.TRANSITION,
                body=body or f"Closed: {task.outcome.value}.",
                data={"lifecycle": "closed", "outcome": task.outcome.value},
                operation=operation,
            )
            return task

        task = self._mutate(task_id, apply)
        self._fire(
            "task.closed",
            task,
            {"triggered_by": actor, "outcome": task.outcome.value if task.outcome else None},
        )
        return task

    def archive_task(self, task_id: str, *, author: Optional[str] = None) -> Task:
        """Hide a task. An open task is closed as cancelled first; archived is a flag."""
        actor = author or "system"
        existing = self._ensure_task_exists(task_id)
        if existing.is_open:
            return self.close_task(
                task_id,
                actor=actor,
                outcome=Outcome.CANCELLED,
                body="Task archived.",
                archive=True,
            )

        def apply(task: Task) -> Task:
            task.archived = True
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.NOTE,
                body="Task archived.",
                data={"archived": True},
            )
            return task

        return self._mutate(task_id, apply)

    # ------------------------------------------------------------------
    # The queue verbs (design doc sections 5 to 8)
    #
    # Reordering is a managed operation, not a content patch. There is no
    # `set_queue_position`, exactly as there is no `set_lifecycle` and for the same
    # reason: the number is a consequence of a decision, and the decision is what the
    # record should show.
    #
    # Every verb below holds the project queue lock and reads the band from disk
    # *inside* it, never from the snapshot cache -- a decision made on a stale band is
    # the bug the lock exists to prevent, and it fails silently by producing two tasks
    # on one number.
    # ------------------------------------------------------------------

    def _require_band_member(self, task_id: str, band: Priority) -> Task:
        """The task a ``before``/``after`` names, refused unless it is in this band.

        Naming a task in a different band is an error rather than an implicit
        reprioritise: those are two different decisions, and conflating them makes the
        log unreadable -- a `queue_move` would silently also be a band change.
        """
        target = self.storage.load_task_uncached(task_id)
        if target is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        if not target.is_open:
            raise ValueError(
                f"Task '{task_id}' is closed, so it is not in line; nothing can be "
                "placed relative to it."
            )
        if target.priority is not band:
            raise ValueError(
                f"Task '{task_id}' is in the '{target.priority.value}' band, not "
                f"'{band.value}'. before/after name a task in the same band -- moving "
                "between bands is reprioritize's decision, not move's."
            )
        return target

    def _place(
        self,
        band: Priority,
        placement: Placement,
        *,
        count: int = 1,
        excluding: Sequence[str] = (),
    ) -> List[int]:
        """The numbers ``count`` tasks take to land at ``placement``. Queue lock held.

        Rebalances the band and asks once more when the chosen gap is exhausted, which
        is the single retry design section 4 allows. A second failure is not a gap
        problem -- a freshly rebalanced band is spaced ``QUEUE_STEP`` apart -- so it is
        reported as what it is rather than retried again.
        """
        tasks = self.storage.list_tasks_uncached()
        positions = plan_insertion(
            band_entries(tasks, band, excluding=excluding), placement, count=count
        )
        if positions is not None:
            return positions

        self._renumber(plan_rebalance(band_entries(tasks, band)))
        tasks = self.storage.list_tasks_uncached()
        positions = plan_insertion(
            band_entries(tasks, band, excluding=excluding), placement, count=count
        )
        if positions is None:
            raise ValueError(
                f"there is no room for {count} tasks at {placement.describe()} of the "
                f"'{band.value}' band even after a rebalance; compact the band or move "
                "fewer tasks at once"
            )
        return positions

    def apply_position(self, task_id: str, position: int) -> Optional[Task]:
        """Write one renumbered position, or skip a task that has since closed.

        The per-task write every renumbering pass goes through, and the reason a
        renumber survives the one writer that does not take the queue lock. ``close``
        deliberately does not lock (design section 5.4), so a task present in the band
        snapshot can close before its write arrives -- and applying the snapshot blindly
        would put a position back onto a closed task, breaking consistency rule 6 in a
        file the renumber itself had just written.

        So openness is re-checked here, under that task's own lock, against a fresh
        read. A skipped task leaves a wider gap, which is the normal state of a sparse
        band; the direction argument in :func:`~agentjobs.queue.plan_renumber` is
        unaffected because skipping a write never reorders anything.

        Returns the task when the position is in place, None when it was skipped. No log
        entry: a renumber records no decision, and forty entries saying "300 became
        1400" would bury the ones that do.
        """

        def apply(task: Task) -> Optional[Task]:
            if not task.is_open or task.queue_position == position:
                return None
            task.queue_position = position
            return task

        try:
            task = self._mutate(task_id, apply)
        except TaskNotFoundError:
            return None
        return task if task.is_open and task.queue_position == position else None

    def _renumber(self, passes: Sequence[RenumberPass]) -> List[Tuple[str, int]]:
        """Apply a renumbering plan in the order it was planned. Queue lock held."""
        applied: List[Tuple[str, int]] = []
        for renumber_pass in passes:
            for task_id, position in renumber_pass:
                if self.apply_position(task_id, position) is not None:
                    applied.append((task_id, position))
        return applied

    def rebalance_band(self, band: Priority) -> List[Tuple[str, int]]:
        """Restore usable spacing in one band without changing anybody's place.

        Automatic on gap exhaustion; exposed because a caller that knows a band is
        crowded may as well say so. Writes no ``queue_move`` entries -- nobody decided
        anything.
        """
        with self.storage.queue_lock():
            entries = band_entries(self.storage.list_tasks_uncached(), band)
            return self._renumber(plan_rebalance(entries))

    def compact_band(self, band: Priority) -> List[Tuple[str, int]]:
        """Renumber a band back to ``100, 200, 300, ...``.

        Explicit only, never automatic, and purely cosmetic: a background process
        quietly rewriting forty task files is exactly the kind of thing that should
        require somebody to type it.
        """
        with self.storage.queue_lock():
            entries = band_entries(self.storage.list_tasks_uncached(), band)
            return self._renumber(plan_compaction(entries))

    def move(
        self,
        task_id: str,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        top: bool = False,
        bottom: bool = False,
        with_children: bool = False,
        actor: str,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
    ) -> Task:
        """Change where a task stands in its band. The only way the order changes.

        Exactly one placement. ``before``/``after`` name a task in the **same band**.

        **The write is one file**, plus the moved children of a group move. That is what
        sparse numbering buys: the mover takes a new number and its neighbours are
        untouched, so a reorder is a one-line diff instead of a forty-file diff that
        conflicts with everything else in flight. If a move is rewriting the band, the
        numbering has been implemented wrong.

        ``with_children`` carries the task's open descendants with it, contiguously, in
        their existing relative order -- and **only the ones in its own band**.
        Contiguity is a within-band property: positions in different bands are never
        compared, so "next to its parent" has no meaning for a ``medium`` child of a
        ``high`` epic. Those children keep their places, and ``moved_with`` names
        exactly who moved, so the record also shows who stayed. A group move never
        changes anyone's priority; that is ``reprioritize``'s decision.
        """
        placement = placement_from(before=before, after=after, top=top, bottom=bottom)
        operation = self._operation(
            operation_id,
            "queue_move",
            actor,
            {
                "placement": placement.as_data(),
                "with_children": with_children,
                "body": body,
            },
        )
        self._ensure_task_exists(task_id)

        with self.storage.queue_lock():
            tasks = self.storage.list_tasks_uncached()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' not found.")
            # Under the queue lock, so a retry that arrives while the first attempt is
            # still writing children waits rather than duplicating their moves.
            if replay_or_conflict(task, operation):
                return task
            check_revision(task, expected_revision)
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is closed, so it is not in line.")

            band = task.priority
            movers = [task, *self._open_descendants_in_band(task_id, tasks, band)]
            if not with_children:
                movers = [task]
            mover_ids = [item.id for item in movers]
            if placement.target is not None:
                if placement.target in mover_ids:
                    raise ValueError(
                        f"Task '{task_id}' cannot be placed relative to '{placement.target}': "
                        "it is one of the tasks being moved."
                    )
                self._require_band_member(placement.target, band)

            positions = self._place(band, placement, count=len(movers), excluding=mover_ids)

            # Children first, so the entry the root ends up carrying names the tasks
            # that actually moved rather than the ones that were planned to. Every
            # intermediate state is still a duplicate-free band: the slots were free
            # before anybody was written into them.
            moved_with = [
                child.id
                for child, position in zip(movers[1:], positions[1:])
                if self.apply_position(child.id, position) is not None
            ]
            data: Dict[str, Any] = {
                "band": band.value,
                "from": task.queue_position,
                "to": positions[0],
                "placement": placement.as_data(),
            }
            if with_children:
                data["moved_with"] = moved_with
            carried = f" with {len(moved_with)} descendant(s)" if moved_with else ""
            return self._write_place(
                task_id,
                positions[0],
                actor=actor,
                body=body or f"Moved to {placement.describe()}{carried}.",
                data=data,
                operation=operation,
                expected_revision=expected_revision,
            )

    def reprioritize(
        self,
        task_id: str,
        priority: Priority,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        top: bool = False,
        actor: str,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        expected_revision: Optional[Union[datetime, str]] = None,
    ) -> Task:
        """Change a task's band and its place in that band, in one decision.

        The default placement in the target band is the **bottom**. A band change
        already says everything about urgency; where inside the new band it lands is a
        separate question the caller may answer explicitly, and "bottom" is the answer
        that assumes least.

        There is no ``with_children`` here on purpose. A group move is about keeping an
        epic next to its work inside one band; carrying children across a band boundary
        would be reprioritising them too, which is a decision each of them deserves in
        its own right and its own log entry.
        """
        band = Priority(priority)
        placement = (
            Placement(Placement.BOTTOM)
            if before is None and after is None and not top
            else placement_from(before=before, after=after, top=top)
        )
        operation = self._operation(
            operation_id,
            "reprioritize",
            actor,
            {"priority": band.value, "placement": placement.as_data(), "body": body},
        )
        self._ensure_task_exists(task_id)

        with self.storage.queue_lock():
            task = self.storage.load_task_uncached(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' not found.")
            if replay_or_conflict(task, operation):
                return task
            check_revision(task, expected_revision)
            if not task.is_open:
                raise ValueError(f"Task '{task_id}' is closed, so it is not in line.")
            if placement.target is not None:
                self._require_band_member(placement.target, band)

            position = self._place(band, placement, excluding=(task_id,))[0]
            data: Dict[str, Any] = {
                "band": band.value,
                "from": task.queue_position,
                "to": position,
                "placement": placement.as_data(),
            }
            if band is not task.priority:
                data["from_band"] = task.priority.value
            where = (
                f"from {task.priority.value} to {band.value}"
                if band is not task.priority
                else f"within {band.value}"
            )
            return self._write_place(
                task_id,
                position,
                priority=band,
                actor=actor,
                body=body or f"Reprioritized {where}, at {placement.describe()}.",
                data=data,
                operation=operation,
                expected_revision=expected_revision,
            )

    def _write_place(
        self,
        task_id: str,
        position: int,
        *,
        actor: str,
        body: str,
        data: Dict[str, Any],
        operation: Optional[Operation],
        expected_revision: Optional[Union[datetime, str]],
        priority: Optional[Priority] = None,
    ) -> Task:
        """One task's new place plus the ``queue_move`` entry that records the decision."""

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            check_revision(task, expected_revision)
            if priority is not None:
                task.priority = priority
            task.queue_position = position
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.QUEUE_MOVE,
                body=body,
                data=data,
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    def _open_descendants_in_band(
        self, task_id: str, tasks: Sequence[Task], band: Priority
    ) -> List[Task]:
        """Open descendants of a task that share its band, in their existing order.

        The whole subtree, not just direct children: an epic's grandchildren are its
        work too. Bands are flat, so a descendant in another band is simply not part of
        this move.
        """
        children: Dict[str, List[Task]] = {}
        for task in tasks:
            if task.parent:
                children.setdefault(task.parent, []).append(task)

        found: List[Task] = []
        seen = {task_id}
        frontier = [task_id]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, []):
                if child.id in seen:
                    continue
                seen.add(child.id)
                frontier.append(child.id)
                if child.is_open and child.priority is band:
                    found.append(child)
        found.sort(key=lambda task: (task.queue_position or 0, task.id))
        return found

    # ------------------------------------------------------------------
    # Corruption is loud (design doc section 8)
    # ------------------------------------------------------------------

    def _queue_places(self) -> List[QueuePlace]:
        """Every open task's claim on a band, including the files that will not load.

        Loaded tasks supply theirs for free. A file that fails to load is then read raw
        -- and that matters, because the commonest corruption, an open task with no
        ``queue_position``, is rejected by consistency rule 6 at load time and would
        otherwise be invisible to a check built on loaded tasks alone.

        The raw reads are bounded by how broken the corpus is, not by how big it is: a
        healthy corpus costs nothing beyond the listing every caller already does.
        """
        loaded = self.storage.load_all()
        places = [place_of(task) for task in loaded.tasks if task.is_open]
        for error in loaded.errors:
            record = read_queue_record(error.path)
            if record is not None and record.is_open:
                places.append(QueuePlace(record.task_id, record.priority, record.queue_position))
        return places

    def assert_queue_integrity(self, bands: Optional[Collection[str]] = None) -> None:
        """Raise :class:`QueueCorruptionError` if the checked bands are not a queue.

        Selection calls this before it answers, over the winning band and the bands
        above it. **Refusing is the point.** A queue that quietly answers while corrupt
        trains everybody to ignore corruption, and the failure it produces -- an agent
        silently working the wrong task -- leaves no trace anywhere.

        Deliberately narrower than the corpus: a duplicate in ``low`` does not falsify
        the claim that a particular ``high`` task is next, and making every selection
        hostage to corruption in a band it never reads would punish the wrong caller.
        ``check_queue`` and ``repair_queue`` cover every band, always.
        """
        problems = find_queue_problems(self._queue_places(), bands=bands)
        if problems:
            raise QueueCorruptionError(problems)

    def check_queue(self) -> List[QueueProblem]:
        """Every queue rule broken anywhere in the corpus. Reports, never raises.

        You have to be able to see a broken queue in order to fix it, which is why this
        and ``repair`` are the two deliberate exceptions to the rule above.
        """
        return find_queue_problems(self._queue_places())

    def repair_queue(self) -> QueueRepairReport:
        """Make a broken queue into a queue again, and say exactly what was guessed.

        Operates on a corrupt corpus by definition, so it reads the raw files rather
        than loaded tasks -- the records it most needs are the ones rule 6 refuses to
        load. Open tasks with no usable position, and the losing claimants of a shared
        one, are placed at the bottom of their band ordered by ``created`` then id: both
        halves immutable, so two runs over one corpus agree.

        It never invents an opinion it does not have. A duplicate position contains no
        record of who was meant to be first, so the tie-break is arbitrary by necessity
        -- and every task it touched is named in the report, which is what makes the
        guess reviewable rather than silent.
        """
        with self.storage.queue_lock():
            directory = self.storage.tasks_dir
            records, _ = read_queue_records(directory)
            open_records = [record for record in records if record.is_open]

            # Who keeps a contested number: earliest created, id breaking the tie. The
            # losers are stripped to None and fall through to the bottom-of-band pass
            # below, which is the same rule the corpus migration used.
            claims: Dict[Tuple[str, int], List[QueueRecord]] = {}
            stripped: List[QueueRecord] = []
            for record in open_records:
                if record.queue_position is None or record.queue_position < 1:
                    stripped.append(record)
                else:
                    claims.setdefault((record.priority, record.queue_position), []).append(record)
            for group in claims.values():
                group.sort(key=baseline_key)
                stripped.extend(group[1:])

            losers = {record.task_id for record in stripped}
            cleaned = [
                record if record.task_id not in losers else replace(record, queue_position=None)
                for record in open_records
            ]
            plan = plan_queue_migration(cleaned)

            unrepairable: List[str] = []
            assigned: List[QueueAssignment] = []
            for assignment in plan.assignments:
                if self._write_raw_position(assignment.task_id, assignment.position):
                    assigned.append(assignment)
                else:
                    unrepairable.append(f"{assignment.task_id}.yaml")

            # Only the bands that were actually broken. Rebalancing a healthy band would
            # rewrite every file in it to change nothing, which is churn a repair has no
            # business producing.
            touched = sorted({assignment.band for assignment in assigned})
            for band in touched:
                entries = band_entries(self.storage.list_tasks_uncached(), Priority(band))
                self._renumber(plan_rebalance(entries))

            return QueueRepairReport(
                assigned=tuple(assigned),
                rebalanced=tuple(touched),
                unrepairable=tuple(unrepairable),
            )

    def _write_raw_position(self, task_id: str, position: int) -> bool:
        """Set one position by rewriting the file, for records that will not load.

        ``mutate_task`` reads the task first, which is precisely what a file missing its
        ``queue_position`` cannot survive -- so repair goes the way the corpus migration
        goes: patch the raw mapping, validate, and save through storage so the file
        comes out canonical with a receipt behind it. False when the file is broken in
        some *other* way, which repair names rather than guesses at.
        """
        path = self.storage.task_path(task_id)
        try:
            raw = load_yaml(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return False
            raw["queue_position"] = position
            self.storage.save_task(Task.model_validate(raw))
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # The log
    # ------------------------------------------------------------------

    def add_log_entry(
        self,
        task_id: str,
        *,
        actor: str,
        type: LogEntryType,
        body: Optional[str] = None,
        re: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Append one entry to the unified log."""
        entry_type = LogEntryType(type)
        if entry_type in MANAGER_WRITTEN_LOG_TYPES:
            raise ValueError(
                f"{entry_type.value} entries are appended by the manager's own verbs, "
                "not written directly (design doc section 3, rule 5)"
            )
        operation = self._operation(
            operation_id,
            "log_append",
            actor,
            {"type": entry_type.value, "body": body, "re": re, "data": data},
        )

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            self._append_entry(
                task,
                actor=actor,
                type=entry_type,
                body=body,
                re=re,
                data=data,
                operation=operation,
            )
            return task

        task = self._mutate(task_id, apply)
        if entry_type is LogEntryType.QUESTION:
            self._fire("task.question", task, {"triggered_by": actor, "body": body})
        return task

    def add_progress_update(
        self,
        task_id: str,
        *,
        author: str,
        summary: str,
        details: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Append a progress entry to the log."""
        body = f"{summary}\n\n{details}" if details else summary
        return self.add_log_entry(
            task_id,
            actor=author,
            type=LogEntryType.PROGRESS,
            body=body,
            operation_id=operation_id,
        )

    # ------------------------------------------------------------------
    # Dispatch
    #
    # The two entry types below are refused by add_log_entry, so these are the only
    # ways to write one. That is deliberate: an entry asserting that a process started
    # or ended must accompany a process actually starting or ending.
    # ------------------------------------------------------------------

    def record_dispatch(
        self,
        task_id: str,
        *,
        actor: str,
        run_id: str,
        agent: str,
        runner: str,
        mode: DispatchMode,
        posture: DispatchPosture,
        trigger: DispatchTrigger,
        caused_by: int,
        argv: List[str],
        cwd: str,
        git_head: str,
        session_id: Optional[str] = None,
        selection: Optional[DispatchSelectionData] = None,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Record that a run was started against this task.

        ``actor`` is the human who authorised it, never the agent that is about to run:
        the loop is human-clocked (D4), and this entry plus ``caused_by`` are the
        evidence. ``argv`` is stored verbatim, so a runner must keep secrets in ``env``.

        ``selection`` is present only when a runner group chose ``runner`` (task-177).
        A flat configuration passes nothing and the entry keeps the shape it has always
        had.
        """
        payload = DispatchData(
            run_id=run_id,
            agent=agent,
            runner=runner,
            mode=mode,
            posture=posture,
            trigger=trigger,
            caused_by=caused_by,
            argv=list(argv),
            cwd=cwd,
            git_head=git_head,
            session_id=session_id,
            selection=selection,
        )
        operation = self._operation(
            operation_id, "dispatch", actor, {"run_id": run_id, "argv": list(argv)}
        )

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.DISPATCH,
                body=body or f"Dispatched {agent} to work this task.",
                data=payload.model_dump(mode="json", exclude_none=True),
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    def record_dispatch_result(
        self,
        task_id: str,
        *,
        actor: str,
        run_id: str,
        outcome: DispatchOutcome,
        re: Optional[int] = None,
        exit_code: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        log_path: Optional[str] = None,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Record how a run ended.

        ``re`` threads this back to the ``dispatch`` entry it concludes. On a successful
        run the body stays empty -- the agent's own progress and handoff entries carry
        the substance, and duplicating a transcript tail into git would be noise. On any
        other outcome the caller is expected to inline the tail of the run's output, so
        the git-tracked record still says something once the machine-local logs are gone.
        """
        payload = DispatchResultData(
            run_id=run_id,
            outcome=outcome,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            log_path=log_path,
        )
        operation = self._operation(
            operation_id,
            "dispatch_result",
            actor,
            {"run_id": run_id, "outcome": outcome.value},
        )

        def apply(task: Task) -> Optional[Task]:
            if replay_or_conflict(task, operation):
                return None
            self._append_entry(
                task,
                actor=actor,
                type=LogEntryType.DISPATCH_RESULT,
                body=body,
                re=re,
                data=payload.model_dump(mode="json", exclude_none=True),
                operation=operation,
            )
            return task

        return self._mutate(task_id, apply)

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def _fire(self, event: str, task: Task, metadata: Dict[str, Any]) -> None:
        """Fire a webhook event when a webhook manager is attached."""
        if self.webhook_manager:
            self.webhook_manager.fire_event(event, task, metadata)
