"""The backend-neutral storage boundary.

The 2026-08-21 storage audit proposed declaring the existing task-store surface as
a Protocol, plus a ``load_raw`` for the four modules that read task files directly. The
spec for task-273 asked for that to be revisited before it was adopted, and it needed
revisiting: **most of that surface is a statement about files, not about storage.**

What a SQL backend is not asked for, and why:

``task_path``
    Every caller of this hands the result to git. There is no file, and returning a
    plausible path would turn a clear failure into a silent one. :class:`SqlTaskStore`
    raises.
``locked`` / ``creation_lock`` / ``queue_lock``
    Three advisory file locks approximating what a transaction *is*. They are kept as
    aliases on the SQL store so callers written against the file backend keep working
    through task-311's cutover, but they are not part of this boundary: new code takes
    a :meth:`TaskStore.transaction`.
``canonical_bytes``
    Existed so the validator could compare a record against the bytes on disk. Bytes on
    disk are no longer the authority, so this is an export concern rather than a
    storage one.
``load_raw``
    The audit wanted it so `queue.py` and `validation.py` could read records too broken
    to validate. Under SQL that requirement inverts: the live tables enforce every
    constraint, so a record too broken to validate **cannot be a row**. It goes to
    ``import_quarantine`` instead, and :meth:`TaskStore.quarantined` is how those are
    read. The file backend keeps its own raw readers; nothing in this Protocol requires
    the SQL one to be able to return an invalid task.

What every backend must do is below. It is deliberately small: eight reads, five
writes, the redaction primitive, and a transaction.
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from .models_v2 import Task, TaskSummary


@runtime_checkable
class TaskStore(Protocol):
    """What the manager, the API, the CLI and MCP require of a storage backend."""

    # ----- reads ------------------------------------------------------------

    def load_task(self, task_id: str) -> Optional[Task]:
        """One task, or ``None`` when there is no task with that id."""

    def list_tasks(self) -> List[Task]:
        """Every task in the project, in listing order."""

    def list_task_summaries(self) -> List[TaskSummary]:
        """Every task as a listing needs it, in the same order as :meth:`list_tasks`.

        Part of the boundary rather than an optimisation inside one backend, because
        the callers that want it -- the list endpoint and every dependency computation
        in the manager -- must not have to know which store they are talking to. A
        backend with no cheaper path answers it by projecting whole records with
        ``summary_of``; a backend with columns reads the columns (task-484).
        """

    def search_tasks(self, query: str) -> List[Task]:
        """Tasks matching free text, most relevant first, exact id matches leading."""

    def project_revision(self) -> Tuple[str, int]:
        """``(token, count)``, where the token changes whenever any task changes.

        Callers poll this, so a backend must be able to answer it without reading
        every record: it is the cheapest question in the product and was the most
        expensive one under files.
        """

    def generate_task_id(self) -> str:
        """The next free task id."""

    def load_errors(self) -> List[Any]:
        """The records that could not be read, as ``TaskLoadError``s.

        Separate from :meth:`load_all` so a caller that wants only the failures does not
        have to load every record that succeeded to get at them. The manager's
        dependency gate wants exactly that: it needs the broken ids so an unreadable
        record can never be treated as done, and it gets the ids it *can* read from
        :meth:`list_task_summaries` (task-484).
        """

    def quarantined(self) -> List[Dict[str, Any]]:
        """Records that could not be loaded, with the error that stopped each.

        Replaces the audit's ``load_raw``. A backend that cannot hold an invalid
        record returns the rejected input here; one that can may return an empty list.
        """

    # ----- writes -----------------------------------------------------------

    def save_task(self, task: Task) -> Task:
        """Persist a whole task, returning what was stored."""

    def mutate_task(self, task_id: str, mutator: Callable[[Task], Optional[Task]]) -> Task:
        """Read, change and write one task atomically.

        The mutator may return ``None`` to decline, which is how a caller checks a
        precondition against state it knows cannot change under it.
        """

    def redact_log_body(self, task_id: str, entry_id: int, body: str) -> None:
        """Rewrite one stored log entry's body, joining the caller's transaction.

        The one exception to an append-only log, and only the redact verb calls it.
        :meth:`save_task` must not persist a changed body on an existing entry, so
        without this a redaction records itself and removes nothing (task-425). Raises
        when there is no such stored entry rather than reporting a removal.
        """

    def delete_task(self, task_id: str) -> bool:
        """Remove a task. ``False`` when there was nothing to remove."""

    def transaction(self) -> ContextManager[Any]:
        """Group several writes so they commit or fail together.

        This is what replaces the three lock methods. Re-entrant: a verb calling
        another verb must join the outer transaction rather than opening a second one,
        or the guarantee it offers is not one.
        """

    def refresh(self) -> None:
        """Discard any cached view of the corpus. A no-op for a backend with none."""


__all__ = ["TaskStore"]
