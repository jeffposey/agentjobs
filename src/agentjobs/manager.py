"""Business logic for managing AgentJobs tasks (schema v2).

The manager owns the state axes. Every change to ``lifecycle``, ``ball``,
``ball_reason``, ``ball_prompt`` or ``outcome`` flows through a verb here --
claim, handoff, release, close -- and every verb appends a log entry, so the
record always shows who moved the ball and why (design doc section 3, rule 5).
Callers never write the axes directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

from .operations import (
    Operation,
    OperationConflictError,
    check_revision,
    find_operation,
    replay_or_conflict,
    stamp,
)

from .models_v2 import (
    Ball,
    BallReason,
    DeliverableStatus,
    DependencyType,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Task,
    utcnow,
)
from .storage import TaskLoadError, TaskStorage

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
        """Compute the claim gate, reverse impact, and cycle errors once."""

        project_tasks = tasks if tasks is not None else self.storage.list_tasks()
        states = self._dependency_states()
        open_children = self._open_children()
        open_children_counts: Dict[str, int] = {}
        for candidate in project_tasks:
            if candidate.parent and candidate.is_open:
                open_children_counts[candidate.parent] = (
                    open_children_counts.get(candidate.parent, 0) + 1
                )
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
                open_children_count=open_children_counts.get(task.id, 0),
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

    def get_next_task(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
    ) -> Optional[Task]:
        """Highest-priority claimable task: ready, eligible, no unmet needs, no open children."""
        tasks = self.storage.list_tasks()
        states = self._dependency_states()
        open_children = self._open_children()
        candidates = [
            task
            for task in tasks
            if task.lifecycle is Lifecycle.READY
            and (priority is None or task.priority == priority)
            and (agent is None or not task.assignment.eligible or agent in task.assignment.eligible)
            and not self._unmet_needs(task, states)
            and task.id not in open_children
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda task: (
                task.priority_rank(),
                -task.updated.timestamp(),
            )
        )
        return candidates[0]

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
        **kwargs: Any,
    ) -> Task:
        """Create a new task, generating an identifier when omitted.

        Tasks are born ``draft`` (ball: human/spec) or ``ready`` (ball:
        agent/available). Any other starting state would skip the transitions the log
        exists to record.

        With an ``operation_id``, creation is idempotent: the whole body runs under a
        project-wide lock, and a retry finds the task the first attempt made instead of
        producing a second one. That lock is what makes an auto-generated id safe --
        two concurrent creates would otherwise both read the same next id, both find
        nothing on disk, and one would overwrite the other.
        """
        if operation_id is None:
            return self._create_unlocked(
                id=id,
                title=title,
                description=description,
                summary=summary,
                priority=priority,
                category=category,
                lifecycle=lifecycle,
                actor=actor,
                operation=None,
                **kwargs,
            )

        operation = Operation(
            id=operation_id,
            kind="create",
            actor=actor or "system",
            payload={"id": id, "title": title, "lifecycle": Lifecycle(lifecycle).value},
        )
        with self.storage.creation_lock():
            existing = self._find_created_by(operation)
            if existing is not None:
                return existing
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
        **kwargs: Any,
    ) -> Task:
        """Build and persist one task. Callers holding the creation lock stay serialised."""
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

        task = Task.model_validate(task_kwargs)
        creator = actor or (operation.actor if operation is not None else None)
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
        """
        operation = self._operation(
            operation_id,
            "update_content",
            actor or "system",
            {"updates": {key: value for key, value in sorted(updates.items())}},
        )

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
            updated = Task.model_validate(payload)
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
        """Take ownership of a ready task, or refuse because someone else already did."""
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
            if children:
                raise ValueError(
                    f"Task '{task_id}' is an umbrella task: it has open sub-tasks "
                    f"({', '.join(children)}). Claim one of those instead -- an umbrella "
                    "is finished by its children, so there is no work to take here."
                )
            task.lifecycle = Lifecycle.ACTIVE
            task.assignment.owner = agent
            task.ball = Ball.AGENT
            task.ball_reason = BallReason.WORK
            task.ball_prompt = "Execute the spec; log progress and hand off when done."
            self._append_entry(
                task,
                actor=agent,
                type=LogEntryType.TRANSITION,
                body=f"Claimed by {agent}.",
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
    ) -> Task:
        """Move the ball. The ask travels with it, by schema requirement."""
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
        if entry_type is LogEntryType.TRANSITION:
            raise ValueError(
                "transition entries are appended by the manager's state verbs, "
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
    # Webhooks
    # ------------------------------------------------------------------

    def _fire(self, event: str, task: Task, metadata: Dict[str, Any]) -> None:
        """Fire a webhook event when a webhook manager is attached."""
        if self.webhook_manager:
            self.webhook_manager.fire_event(event, task, metadata)
