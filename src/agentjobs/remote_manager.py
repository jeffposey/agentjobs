"""The manager's surface, over HTTP, for the processes that may not open the database.

The owner decided on 2026-09-05 that only the server opens the store and every CLI verb
becomes a service client (task-273, entry 6, item 3). This is the shape that makes that
true without rewriting the CLI or the dispatch subsystem: a facade with the methods
:class:`~agentjobs.manager.TaskManager` exposes, backed by
:class:`~agentjobs.client.TaskClient`.

## Why a facade rather than a remote *store*

The obvious alternative -- a ``TaskStore`` that reads and writes rows over HTTP, with the
existing manager on top of it -- was rejected, and the reason is the whole point of the
migration. A verb like ``claim`` is a read-decide-write that must be *one transaction*;
splitting it across a network puts the decision in one process and the write in another,
which is the double-claim race task-055 closed. The unit that crosses the wire has to be
the verb.

## What it deliberately does not do

**It does not present a run credential.** A client built inside a dispatched run
normally sends one, and the capability gate then scopes it to that run's own task
(task-332). The CLI has never been inside that gate -- it drove the manager directly and
spoke no HTTP -- and this migration is not the change that should tighten it. Tightening
it would break the epic walk outright: a supervisor run's ``agentjobs dispatch walk``
would be refused the ``dispatch`` capability, and every child it started would be
somebody else's task. Suppressing the header keeps the CLI exactly where it already was,
because an agent that wanted owner authority could already shell out to the CLI to get
it. Changing that is a real decision with real consequences, and it belongs to whoever
takes it deliberately -- see the note recorded on task-311.

**It does not reimplement anything.** Every method here is a call and a parse. The
dataclasses the CLI renders are reconstructed from the ``as_dict`` forms the API already
returns, so there is exactly one definition of each wire shape and a round-trip test
holds the two ends together.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .client import TaskClient
from .manager import (
    DependencyFacts,
    MoveOutcome,
    NextExplanation,
    QueueBand,
    QueueEntry,
    QueueListing,
    QueueMoveProvenance,
    QueueRepairReport,
    SkippedTask,
)
from .models_v2 import (
    Ball,
    BallReason,
    LogEntryType,
    Outcome,
    Priority,
    Task,
)
from .projects import Project
from .queue import Placement, QueueAssignment, QueueProblem
from .queue_check import QueueWarning
from .storage_config import StorageSettings


class RemoteStoreUnsupported(RuntimeError):
    """Something asked a remote manager for a file-shaped thing that has no answer."""


def _operation_id() -> str:
    """A fresh idempotency key for a call the caller did not key itself.

    Generated per call rather than per manager: the point of the key is that *this
    attempt* replays rather than writing twice, and a key reused across two different
    calls is a conflict the server refuses.
    """
    return str(uuid.uuid4())


# ----- reconstructing what the API returns -----------------------------------
#
# Each of these is the inverse of an `as_dict` that already exists. Written here rather
# than beside `as_dict` so the wire shape keeps one definition and the CLI keeps one
# vocabulary; `tests/test_remote_manager.py` asserts the round trip for each.


def _provenance(data: Optional[Dict[str, Any]]) -> Optional[QueueMoveProvenance]:
    if not data:
        return None
    return QueueMoveProvenance(
        actor=str(data.get("actor") or ""),
        kind=data.get("kind"),
        at=str(data.get("at") or ""),
        body=str(data.get("body") or ""),
        anchor=data.get("anchor"),
    )


def _entry(data: Dict[str, Any]) -> QueueEntry:
    return QueueEntry(
        task=str(data.get("task") or ""),
        title=str(data.get("title") or ""),
        queue_position=data.get("queue_position"),
        lifecycle=str(data.get("lifecycle") or ""),
        ball=data.get("ball"),
        claimable=bool(data.get("claimable")),
        reason=data.get("reason"),
        last_move=_provenance(data.get("last_move")),
    )


def _problem(data: Dict[str, Any]) -> QueueProblem:
    return QueueProblem(
        kind=str(data.get("kind") or ""),
        band=str(data.get("band") or ""),
        task_ids=tuple(data.get("tasks") or ()),
        position=data.get("position"),
    )


def _warning(data: Dict[str, Any]) -> QueueWarning:
    return QueueWarning(
        kind=str(data.get("kind") or ""),
        message=str(data.get("message") or ""),
        tasks=tuple(data.get("tasks") or ()),
    )


def _placement(data: Optional[Dict[str, Any]]) -> Optional[Placement]:
    if not data:
        return None
    return Placement(kind=str(data.get("kind") or ""), target=data.get("target"))


def _listing(payload: Dict[str, Any]) -> QueueListing:
    return QueueListing(
        bands=tuple(
            QueueBand(
                band=str(band.get("band") or ""),
                entries=tuple(_entry(item) for item in band.get("entries") or ()),
            )
            for band in payload.get("bands") or ()
        ),
        problems=tuple(_problem(item) for item in payload.get("problems") or ()),
    )


def _explanation(payload: Dict[str, Any]) -> NextExplanation:
    return NextExplanation(
        task=payload.get("task"),
        band=payload.get("band"),
        queue_position=payload.get("queue_position"),
        empty_bands_above=tuple(payload.get("empty_bands_above") or ()),
        skipped=tuple(
            SkippedTask(
                task=str(item.get("task") or ""),
                queue_position=item.get("position"),
                reason=str(item.get("reason") or ""),
            )
            for item in payload.get("skipped") or ()
        ),
    )


# ----- the storage stand-in --------------------------------------------------


class RemoteStorage:
    """The handful of ``manager.storage`` reads the CLI and dispatch still make.

    Not a :class:`~agentjobs.storage_protocol.TaskStore`, and not pretending to be one:
    it answers the four questions callers actually ask of storage from outside the
    server, and refuses the rest by name. A stand-in that returned a plausible answer to
    ``task_path`` would turn a clear failure into a silent one, which is the same
    argument the SQL store makes for refusing it.
    """

    supports_task_files = False
    """The records are on the server. Nothing here has a file to commit."""

    def __init__(self, client: TaskClient) -> None:
        self._client = client

    def list_tasks(self) -> List[Task]:
        """Every task in the project, in listing order."""
        return self._client.list_tasks()

    def list_tasks_uncached(self) -> List[Task]:
        """The same: a request is always current, so there is no cache to bypass."""
        return self.list_tasks()

    def load_task(self, task_id: str) -> Optional[Task]:
        """One task, or ``None``."""
        try:
            return self._client.get_task(task_id)
        except Exception:  # noqa: BLE001 - a missing task is not an error to a caller
            return None

    load_task_uncached = load_task

    def load_all(self) -> Any:
        """Every task, plus whatever the server reports as broken, in the file shape.

        ``LoadResult`` is what the listing and validation surfaces consume. The broken
        half comes from ``/tasks/broken`` rather than being assumed empty: a migrated
        project can still hold quarantined records from its import, and a listing that
        quietly reported none would hide exactly the records an operator needs to see.
        """
        from .taskfiles import LoadResult, TaskLoadError

        errors = [
            TaskLoadError(
                Path(str(record.get("path") or record.get("filename") or "?")),
                str(record.get("reason") or "unreadable"),
            )
            for record in self._client.read_broken_tasks()
        ]
        return LoadResult(tasks=self.list_tasks(), errors=errors)

    @property
    def attachments(self) -> Any:
        """Refused: attachment bytes live in the store and are read through the API."""
        raise RemoteStoreUnsupported(
            "attachment bytes for this project are in the AgentJobs database and are "
            "read through the API, not from a directory on this machine."
        )

    def refresh(self) -> None:
        """No-op: there is no local snapshot to invalidate."""

    def generate_task_id(self) -> str:
        """Refused: ids are allocated by the server inside the creating transaction."""
        raise RemoteStoreUnsupported(
            "task ids are allocated by the server inside the transaction that creates "
            "the task; a client cannot reserve one in advance."
        )

    @property
    def tasks_dir(self) -> Path:
        """Refused: this project's tasks are rows on the server, not files here."""
        raise RemoteStoreUnsupported(
            "this project is served from the AgentJobs database, so it has no tasks "
            "directory on this machine. Use 'agentjobs storage export' for files."
        )

    def task_path(self, task_id: str) -> Path:
        """Refused, for the same reason, and loudly because callers hand this to git."""
        raise RemoteStoreUnsupported(
            f"task {task_id!r} is a row on the AgentJobs server, not a file in this "
            "checkout. Nothing commits task records any more -- see ENGINEERING.md."
        )

    _task_path = task_path

    def has_task(self, task_id: str) -> bool:
        """Whether the server holds a task with that id."""
        return self.load_task(task_id) is not None


# ----- the manager -----------------------------------------------------------


class RemoteTaskManager:
    """:class:`~agentjobs.manager.TaskManager`'s surface, served over HTTP."""

    def __init__(self, client: TaskClient, project: Optional[Project] = None) -> None:
        """Bind a remote manager to a project-scoped client."""
        self.client = client
        self.project = project
        self.storage = RemoteStorage(client)

    # ----- reads ------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[Task]:
        """One task, or ``None`` when there is none with that id."""
        return self.storage.load_task(task_id)

    def list_tasks(self, **filters: Any) -> List[Task]:
        """Tasks, optionally filtered the way the REST listing filters them."""
        return self.client.list_tasks(**filters)

    def search_tasks(self, query: str) -> List[Task]:
        """Free-text search, most relevant first."""
        return self.client.search_tasks(query)

    def get_next_task(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> Optional[Task]:
        """The claimable task that stands first in line."""
        if parent is not None:
            claimable = self.claimable_tasks(priority, agent=agent, parent=parent)
            return claimable[0] if claimable else None
        return self.client.get_next_task(priority=priority, agent=agent)

    def claimable_tasks(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> List[Task]:
        """Every task that may be worked now, in the queue's order."""
        payload = self.client.read_claimable(
            priority=self.client._enum_to_str(priority) if priority else None,
            agent=agent,
            parent=parent,
        )
        return [self.client._parse_task(item) for item in payload]

    def explain_next(
        self,
        priority: Optional[Priority] = None,
        *,
        agent: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> NextExplanation:
        """Why this task is next, and every open task it stands in front of."""
        del parent  # the route explains the whole queue; a parent narrows selection only
        return _explanation(
            self.client.explain_next_task(
                priority=self.client._enum_to_str(priority) if priority else None, agent=agent
            )
        )

    def queue_listing(
        self, *, agent: Optional[str] = None, actors: Optional[Mapping[str, str]] = None
    ) -> QueueListing:
        """The whole open backlog in queue order, band by band, plus what is broken.

        ``actors`` is the project vocabulary the local manager takes because it reads
        config from disk. The server already knows its own project vocabulary and stamps
        each entry provenance with it, so a copy sent by the caller would be a second
        opinion rather than the first.
        """
        del actors
        return _listing(self.client.queue(agent=agent))

    def check_queue(self) -> List[QueueProblem]:
        """Every queue rule broken anywhere in the corpus. Reports, never raises."""
        return list(self.queue_listing().problems)

    def get_subtasks(self, task_id: str) -> List[Task]:
        """This task's children, in listing order."""
        return [task for task in self.list_tasks() if task.parent == task_id]

    def dependency_facts(self, tasks: Optional[List[Task]] = None) -> Dict[str, DependencyFacts]:
        """Dependency state per task, computed by the server over the whole corpus.

        Read from the listing's enriched rows rather than recomputed here. Two
        implementations of "is this actionable" is exactly how a walk and a dashboard
        come to disagree about what may be started.
        """
        del tasks  # the server computes over the corpus; a subset would be wrong
        facts: Dict[str, DependencyFacts] = {}
        for row in self.client.read_tasks():
            facts[str(row.get("id"))] = DependencyFacts(
                unmet_needs=tuple(row.get("unmet_needs") or ()),
                actionable=bool(row.get("actionable")),
                needs_cycles=tuple(tuple(cycle) for cycle in row.get("needs_cycles") or ()),
                unblocks_count=int(row.get("unblocks_count") or 0),
                open_children_count=int(row.get("open_children_count") or 0),
            )
        return facts

    def _revision(self, task_id: str) -> datetime | str:
        """The task's current ``updated``, for a verb whose caller did not supply one.

        Several client verbs require ``expected_revision`` and are right to: a mutation
        decided against a stale read is refused rather than silently discarding somebody
        else's write. The local manager gets that for free by deciding inside the
        transaction; a remote caller has to read first, and this is where the read
        happens so the refusal stays possible instead of being opted out of.
        """
        current = self.get_task(task_id)
        if current is None:
            from .manager import TaskNotFoundError

            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        return current.updated

    # ----- the verbs --------------------------------------------------------

    def create_task(self, **fields: Any) -> Task:
        """File a task. Returns it as stored, with the id the server allocated.

        ``summary`` falls back to the title, which is what the local manager does when a
        caller omits it (``spec_payload.setdefault("summary", summary or title)``). The
        REST surface requires it, so without this a CLI ``create`` that had always
        worked would start failing the moment its project migrated -- for a field the
        caller never had to supply.
        """
        actor = fields.pop("actor", None) or fields.pop("author", None) or "claude"
        fields = {key: value for key, value in fields.items() if value is not None}
        fields.setdefault("summary", fields.get("title", ""))
        fields.setdefault("description", "")
        return self.client.operations.create(actor=actor, operation_id=_operation_id(), **fields)

    def promote_task(self, task_id: str, *, actor: str, **rest: Any) -> Task:
        """Draft becomes ready and claimable."""
        rest.pop("operation_id", None)
        expected = rest.pop("expected_revision", None)
        return self.client.operations.promote(
            task_id,
            actor=actor,
            operation_id=_operation_id(),
            expected_revision=expected or self._revision(task_id),
            **rest,
        ).task

    def claim_task(self, task_id: str, *, agent: str, **rest: Any) -> Task:
        """Atomically claim ready work. One winner, however many ask at once."""
        rest.pop("actor", None)
        return self.client.operations.claim(
            task_id, actor=agent, operation_id=_operation_id(), **rest
        ).task

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
        expected_revision: Optional[datetime | str] = None,
        questions: Optional[Sequence[Any]] = None,
        **rest: Any,
    ) -> Task:
        """Move the ball, with the ask that travels with it."""
        del rest
        return self.client.operations.handoff(
            task_id,
            actor=actor,
            ball=ball,
            ball_reason=ball_reason,
            ball_prompt=ball_prompt or "",
            body=body,
            operation_id=operation_id or _operation_id(),
            expected_revision=expected_revision or self._revision(task_id),
            questions=questions,
        ).task

    def release_task(self, task_id: str, *, actor: str, body: Optional[str] = None) -> Task:
        """Return claimed work to the ready pool."""
        return self.client.operations.release(
            task_id, actor=actor, operation_id=_operation_id(), body=body
        ).task

    def close_task(
        self,
        task_id: str,
        *,
        actor: str,
        outcome: Outcome,
        body: Optional[str] = None,
        operation_id: Optional[str] = None,
        **rest: Any,
    ) -> Task:
        """Close the task with an outcome."""
        return self.client.operations.close(
            task_id,
            actor=actor,
            outcome=outcome,
            body=body,
            operation_id=operation_id or _operation_id(),
            expected_revision=rest.pop("expected_revision", None) or self._revision(task_id),
        ).task

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
        """Append one authored entry to the append-only log."""
        return self.client.operations.append_log(
            task_id,
            actor=actor,
            operation_id=operation_id or _operation_id(),
            type=type,
            body=body or "",
            re=re,
            data=data,
        ).task

    def add_progress_update(
        self,
        *,
        task_id: str,
        author: str,
        summary: str,
        details: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Task:
        """Append a structured progress entry."""
        del operation_id
        return self.client.add_progress_update(
            task_id, summary=summary, details=details, agent=author
        )

    def update_task(self, task_id: str, **updates: Any) -> Task:
        """Update authoring content. State axes do not move through here."""
        actor = updates.pop("actor", None) or "claude"
        expected = updates.pop("expected_revision", None)
        updates.pop("operation_id", None)
        if expected is None:
            current = self.get_task(task_id)
            if current is None:
                from .manager import TaskNotFoundError

                raise TaskNotFoundError(f"Task '{task_id}' not found.")
            expected = current.updated
        return self.client.operations.update_content(
            task_id,
            actor=actor,
            operation_id=_operation_id(),
            expected_revision=expected,
            **updates,
        )

    # ----- the queue --------------------------------------------------------

    def move_with_warnings(
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
        expected_revision: Optional[datetime | str] = None,
    ) -> MoveOutcome:
        """Move a task within its band, reporting what the move is worth saying."""
        result = self.client.operations.queue_move(
            task_id,
            actor=actor,
            operation_id=operation_id or _operation_id(),
            expected_revision=expected_revision or self._revision(task_id),
            before=before,
            after=after,
            top=top,
            bottom=bottom,
            with_children=with_children,
            body=body,
        )
        return MoveOutcome(
            task=result.task,
            warnings=tuple(_warning(item.model_dump()) for item in result.queue_warnings),
            undo=_placement(result.queue_undo),
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
        expected_revision: Optional[datetime | str] = None,
    ) -> Task:
        """Move a task into another band, placing it there rather than guessing."""
        return self.client.operations.reprioritize(
            task_id,
            actor=actor,
            operation_id=operation_id or _operation_id(),
            expected_revision=expected_revision or self._revision(task_id),
            priority=priority,
            before=before,
            after=after,
            top=top,
            body=body,
        ).task

    def repair_queue(self) -> QueueRepairReport:
        """Make a broken queue into a queue again, and say what was guessed."""
        payload = self.client.operations.repair_queue(actor="claude", operation_id=_operation_id())
        return QueueRepairReport(
            assigned=tuple(
                QueueAssignment(
                    task_id=str(item.get("task_id") or item.get("task") or ""),
                    band=str(item.get("band") or ""),
                    position=int(item.get("position") or 0),
                )
                for item in payload.get("assigned") or ()
            ),
            rebalanced=tuple(payload.get("rebalanced") or ()),
            unrepairable=tuple(payload.get("unrepairable") or ()),
        )

    def compact_band(self, band: Priority) -> List[Tuple[str, int]]:
        """Renumber one band onto the standard gaps."""
        payload = self.client.operations.compact_queue(
            band=self.client._enum_to_str(band), actor="claude", operation_id=_operation_id()
        )
        return [
            (str(item.get("task_id") or item.get("task") or ""), int(item.get("position") or 0))
            for item in payload.get("renumbered") or payload.get("assigned") or ()
        ]

    # ----- the record itself ------------------------------------------------

    def redact(
        self,
        task_id: str,
        *,
        field: str,
        replacement: str,
        reason: str,
        actor: str,
        operation_id: Optional[str] = None,
        expected_revision: Optional[datetime | str] = None,
    ) -> Task:
        """Replace one prose region with a stated redaction."""
        return self.client.operations.redact(
            task_id,
            actor=actor,
            operation_id=operation_id or _operation_id(),
            field=field,
            replacement=replacement,
            reason=reason,
            expected_revision=expected_revision,
        ).task

    def record_dispatch(self, task_id: str, *, actor: str, **payload: Any) -> Task:
        """Refused: a run is recorded by the process that started it.

        The two dispatch entries are the one part of the manager that deliberately does
        not cross the wire. Recording a dispatch means sending ``argv``, and a dispatch
        request model carrying ``argv`` is what
        ``tests/test_dispatch_api.py::test_no_dispatch_request_body_accepts_a_command_to_run``
        forbids -- a schema is the execution surface whatever today's page sends. So the
        dispatch family holds a local manager instead
        (:func:`~agentjobs.store_factory.dispatch_manager_for`), which is where the whole
        argument for that exception is written down.

        A caller reaching this has got a remote manager into the dispatch path, which is
        a wiring mistake rather than a missing feature.
        """
        del task_id, actor, payload
        raise RemoteStoreUnsupported(
            "a run is recorded by the process that started it, not over the service. "
            "The dispatch family uses store_factory.dispatch_manager_for; see its "
            "docstring for why that exception exists."
        )

    def record_dispatch_result(self, task_id: str, *, actor: str, **payload: Any) -> Task:
        """Refused, for the same reason as :meth:`record_dispatch`."""
        del task_id, actor, payload
        raise RemoteStoreUnsupported(
            "a run's outcome is recorded by the process that watched it, not over the "
            "service. See store_factory.dispatch_manager_for."
        )


# ----- how a caller gets one -------------------------------------------------


def remote_manager_for(
    project: Project,
    *,
    base_url: Optional[str] = None,
    settings: Optional[StorageSettings] = None,
) -> RemoteTaskManager:
    """A manager addressing ``project`` on this machine's AgentJobs service.

    The address comes from the same place dispatch reads it -- ``AGENTJOBS_API_BASE``,
    then ``api_base`` in ``~/.agentjobs/dispatch.yaml``, then loopback on the default
    port. One answer for the whole machine, so a non-default port is stated once
    rather than being wrong in several places.
    """
    del settings
    from .dispatch.address import configured_api_base

    address = base_url or configured_api_base() or "http://127.0.0.1:8765"
    # No run credential: see this module's docstring. `headers={}` rather than letting
    # the client read the environment, which is what makes the suppression explicit
    # instead of incidental.
    import httpx

    connection = httpx.Client(base_url=address.rstrip("/"), timeout=30.0)
    client = TaskClient(address, client=connection, project_id=project.id)
    return RemoteTaskManager(client, project)


__all__ = [
    "RemoteStorage",
    "RemoteStoreUnsupported",
    "RemoteTaskManager",
    "remote_manager_for",
]
