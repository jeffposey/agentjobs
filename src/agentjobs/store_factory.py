"""One place a project id becomes a task store.

Before the cutover there were twenty-odd ``TaskStorage(project.tasks_dir())`` calls
scattered across the CLI, the API, dispatch, the queue tools and the validator. Each was
a small independent decision that task state is files in a directory. This module is the
single decision they now defer to, so switching a project's authority is a line in
``storage.yaml`` rather than an audit of every call site.

## Only the server opens the database

That is the owner's decision of 2026-09-05 (task-273, entry 6), and it is enforced here
rather than merely written down. :func:`open_store` refuses to open SQLite unless the
calling process has declared itself the server, and the refusal names the service the
caller should have used. Two processes opening one SQLite file is exactly the
concurrency the design exists to remove, and a rule that lives only in prose is one that
holds until somebody adds a call site in a hurry.

The escape hatch is deliberate and narrow: :func:`server_process` is a context manager
that the API app enters, and that the import, backup and restore tooling enters for the
duration of an operation an operator ran with the server stopped. Those tools *are* the
only writer while they hold it, which is the same guarantee under a different process.

## The database is opened once per path

``Database`` owns a write connection, so a process that opened two of them against one
file would defeat the single-writer rule from the inside. The cache here is keyed on the
resolved path and is what makes "the server has one database" true no matter how many
projects it serves -- one store per project, one database under all of them, which is
the shape the physical schema was designed for (``project_id`` on every key).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, Optional, Union

from .__version__ import __version__
from .projects import Project
from .sqlstore import SqlTaskStore
from .sqlstore.connection import Database
from .sqlstore.migrations import upgrade
from .storage import TaskStorage
from .storage_config import StorageSettings, load_storage_settings

if TYPE_CHECKING:  # pragma: no cover - both import this module at runtime
    from .manager import TaskManager
    from .remote_manager import RemoteTaskManager

TaskStoreBackend = Union[TaskStorage, SqlTaskStore]

TaskManagerLike = Union["TaskManager", "RemoteTaskManager"]
"""Either manager. The two are the same surface reached two ways, and a caller that
holds one is not entitled to know which -- which is the property that let the CLI and
the dispatch subsystem cross over without being rewritten.
"""


class StoreAccessError(RuntimeError):
    """A process that is not the server tried to open the database directly."""


_server_depth = 0
_server_lock = threading.Lock()

_databases: Dict[str, Database] = {}
_database_lock = threading.Lock()


# ----- who is allowed to open the database ----------------------------------


def mark_server_process() -> None:
    """Declare this process the one that owns the database.

    Called by the API application factory. Not paired with an "unmark": a process that
    has become the server stays the server for its lifetime, and the tests that need to
    take the declaration away use :func:`reset_server_process`.
    """
    global _server_depth
    with _server_lock:
        _server_depth += 1


def reset_server_process() -> None:
    """Forget the declaration. For tests, and for nothing else."""
    global _server_depth
    with _server_lock:
        _server_depth = 0


def is_server_process() -> bool:
    """True when this process may open the database directly."""
    return _server_depth > 0


@contextmanager
def server_process() -> Iterator[None]:
    """Hold the server declaration for the duration of a block.

    What the import, backup, restore and export tooling uses. Those run with the server
    stopped -- the cutover quiesces writers before its final import -- and while one is
    running it is the single writer, which is the property the rule is protecting.
    """
    mark_server_process()
    try:
        yield
    finally:
        global _server_depth
        with _server_lock:
            _server_depth = max(0, _server_depth - 1)


# ----- the database ---------------------------------------------------------


def open_database(
    path: Optional[Path] = None, *, settings: Optional[StorageSettings] = None
) -> Database:
    """The process's ``Database`` for one file, opened on first use.

    Migrations are applied on open, which is the only moment they can be: the schema a
    connection needs is the schema the code it belongs to was written against, and
    deferring that to a separate command produces a server that starts fine and fails on
    its first query.
    """
    resolved = Path(path) if path is not None else (settings or load_storage_settings()).database
    key = str(Path(resolved).expanduser().resolve())
    with _database_lock:
        existing = _databases.get(key)
        if existing is not None:
            return existing
        database = Database(Path(key))
        upgrade(database, agentjobs_version=__version__)
        _databases[key] = database
        return database


def close_databases() -> None:
    """Close every database this process opened.

    The server calls this on shutdown so SQLite checkpoints the WAL and runs
    ``PRAGMA optimize`` rather than leaving both to the next start. Tests call it
    between temporary databases.
    """
    with _database_lock:
        opened = list(_databases.items())
        _databases.clear()
    for _, database in opened:
        try:
            database.close()
        except Exception:  # pragma: no cover - a close that fails must not mask the exit
            pass


# ----- the factory ----------------------------------------------------------


def open_store(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    tasks_dir: Optional[Path] = None,
) -> TaskStoreBackend:
    """The task store for one project, according to this machine's configuration.

    ``tasks_dir`` overrides where the file backend looks. The API passes it because a
    project's directory is created on demand there and because the implicit
    single-project mode resolves it from the environment rather than from the registry.
    """
    resolved = settings or load_storage_settings()
    if not resolved.on_sqlite(project.id):
        return TaskStorage(Path(tasks_dir) if tasks_dir is not None else project.tasks_dir())

    if not is_server_process():
        raise StoreAccessError(
            f"Project {project.id!r} is served from SQLite, and only the AgentJobs "
            "server opens that database. Reach it over the service instead of opening "
            "storage directly -- start the server with 'agentjobs serve', or use "
            "'agentjobs storage' for the operator commands that legitimately hold the "
            "store while the server is stopped."
        )

    database = open_database(resolved.database)
    store = SqlTaskStore(database, project.id)
    store.ensure_project(root=str(project.root))
    return store


def task_manager_for(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    webhook_manager: Optional[Any] = None,
) -> TaskManagerLike:
    """A manager for one project, local or remote according to who is asking.

    This is what every call site outside the API now uses, and it is deliberately the
    *only* branch in the product between the two worlds:

    -   A project on files, or any project inside the server process, gets a real
        :class:`~agentjobs.manager.TaskManager` over the store this machine configured.
    -   A project on SQLite, asked for from anywhere else, gets a
        :class:`~agentjobs.remote_manager.RemoteTaskManager` -- the same surface, over
        the service, because only the server opens the database.

    A caller therefore does not have to know which world it is in, which is what keeps
    the CLI's twelve construction sites from each becoming a decision. What a caller
    *cannot* do either way is read a directory the machine no longer considers
    authoritative: that is the failure this replaces, and it was silent.
    """
    from .manager import TaskManager

    resolved = settings or load_storage_settings()
    if resolved.on_sqlite(project.id) and not is_server_process():
        from .remote_manager import remote_manager_for

        return remote_manager_for(project, settings=resolved)
    return TaskManager(open_store(project, settings=resolved), webhook_manager)


def dispatch_manager_for(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    webhook_manager: Optional[Any] = None,
) -> "TaskManager":
    """A **local** manager, for the dispatch subsystem only. The one exception.

    The owner's decision of 2026-09-05 (task-273, entry 6, item 3) is that every CLI
    verb becomes a service client so that only the server opens the database. This
    function is a documented departure from that for the dispatch family, and the
    reason is that honouring it literally there requires weakening three guards that
    three separate tasks built deliberately:

    1.  **The run-credential scope.** A client built inside a dispatched run presents
        its credential, and the capability gate then scopes it to that run's own task
        (task-332). ``agentjobs dispatch walk`` is run *by* a supervisor session and
        starts *other* tasks' runs; over HTTP every one of those is ``wrong_task``, and
        the epic walk stops working. `docs/authorization.md` states the CLI is outside
        that gate "by construction", and this migration is not the change that should
        alter who is trusted.
    2.  **``argv`` never crosses the wire.** Recording a dispatch over HTTP means a
        request model carrying ``argv``, and
        ``tests/test_dispatch_api.py::test_no_dispatch_request_body_accepts_a_command_to_run``
        forbids exactly that -- a schema is the surface, whatever today's page happens
        to send.
    3.  **One authorization per dispatch.** Routing the CLI through
        ``POST /tasks/{id}/dispatch`` instead needs ``trigger`` and
        ``on_behalf_of_parent`` on its body, which is what
        ``DispatchRequestBody._one_authorization`` exists to refuse: it is how a caller
        would claim a run is a child riding a parent's authorization when it is not.

    **What makes the exception safe rather than merely convenient.** The decision's
    stated reason is that "two processes opening one SQLite file is exactly the
    concurrency the design exists to remove". That is a good default and it is not a
    correctness argument for *this* store: WAL plus ``busy_timeout`` is what SQLite
    provides multi-process access with, every invariant is a ``CHECK`` or a unique index
    **in the database** rather than in a Python validator, ``BEGIN IMMEDIATE`` takes the
    write lock up front, and the operation ledger that makes a retry a replay is a table.
    ``connection.py`` sets the busy timeout for precisely this reason -- it says so.

    **The narrowness is the point.** Only the dispatch family calls this, one place says
    so, and everything else -- every task verb, every queue verb, the CLI a person types
    at -- goes over the service as decided.

    It refuses when the database needs a migration this process would have to apply:
    two processes racing schema changes is a real hazard rather than a theoretical one,
    and the server is the process that should apply them.
    """
    from .manager import TaskManager

    resolved = settings or load_storage_settings()
    if resolved.on_sqlite(project.id) and not is_server_process():
        _assert_schema_current(resolved.database)
        with server_process():
            return TaskManager(open_store(project, settings=resolved), webhook_manager)
    return TaskManager(open_store(project, settings=resolved), webhook_manager)


def _assert_schema_current(database: Path) -> None:
    """Refuse a direct open when the store is behind this build's physical schema.

    Applying a migration from a short-lived CLI process, possibly while a server holds
    the file, is the one thing multi-process SQLite does not make safe. The repair is
    the ordinary one: start the server, which applies migrations on open.
    """
    import sqlite3

    from .sqlstore.migrations import latest_version

    if not Path(database).exists():
        return
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        have = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    want = latest_version()
    if have < want:
        raise StoreAccessError(
            f"the database at {database} is at physical schema version {have} and this "
            f"build expects {want}. Start the AgentJobs server, which applies migrations "
            "on open, and run this again -- a short-lived process must not apply a "
            "schema change that another process may be reading through."
        )


def store_is_sql(store: object) -> bool:
    """True when this store is the SQLite one.

    A predicate rather than ``isinstance`` at each call site, so the handful of places
    that still have to care -- queue repair over raw files, receipt checks, the
    validator's byte comparison -- name *what* they are asking rather than which class
    they happened to get.
    """
    return isinstance(store, SqlTaskStore)


__all__ = [
    "StoreAccessError",
    "TaskStoreBackend",
    "close_databases",
    "is_server_process",
    "mark_server_process",
    "open_database",
    "open_store",
    "reset_server_process",
    "server_process",
    "store_is_sql",
    "TaskManagerLike",
    "dispatch_manager_for",
    "task_manager_for",
]
