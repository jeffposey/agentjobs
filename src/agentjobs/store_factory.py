"""One place a project id becomes a task store.

Before the cutover there were twenty-odd ``TaskStorage(project.tasks_dir())`` calls
scattered across the CLI, the API, dispatch, the queue tools and the validator. Each was
a small independent decision that task state is files in a directory. This module is the
single decision they now defer to.

**There is one kind of store** (task-402). What is left to resolve is *which database
file* a project's rows are in, and *whether this process may open it at all* -- not which
backend it is on. A caller that used to branch on the answer no longer has one to branch
on.

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
resolved path, so a file is opened once however many projects resolve to it.

**Which file is a question about a project, not about the machine** (task-400). Every
project has its own database by default; the physical schema still carries
``project_id`` on every key, so two projects sharing one file remains a configuration an
operator can choose, and one that already exists on machines cut over before that
change. :func:`open_database` therefore takes a path and nothing else -- the caller has
to have asked :meth:`~agentjobs.storage_config.StorageSettings.database_for` which one.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, Optional, Union

from .__version__ import __version__
from .projects import Project, default_home
from .sqlstore import SqlTaskStore
from .sqlstore.connection import Database
from .sqlstore.migrations import upgrade
from .storage_config import DATABASES_DIRNAME, StorageSettings, load_storage_settings

if TYPE_CHECKING:  # pragma: no cover - both import this module at runtime
    from .manager import TaskManager
    from .remote_manager import RemoteTaskManager

TaskStoreBackend = SqlTaskStore
"""The task store. A name rather than a union, kept because a hundred annotations say
it and because "the store this machine serves" is still worth naming."""

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


def open_database(path: Path) -> Database:
    """The process's ``Database`` for one file, opened on first use.

    Migrations are applied on open, which is the only moment they can be: the schema a
    connection needs is the schema the code it belongs to was written against, and
    deferring that to a separate command produces a server that starts fine and fails on
    its first query.

    The path is required. It used to default to the machine's one database, and after
    task-400 there is no such thing to default to: a caller with a project in hand asks
    ``settings.database_for(project.id)``, and one without has no business opening
    anything.
    """
    key = str(Path(path).expanduser().resolve())
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


LOCAL_PROJECT_ID = "_local"
"""The project id for a directory nobody registered.

Shared by the server's implicit single-project mode and by the CLI's fallback, and it
has to be the same string in both: they address one database, and a store is keyed on
(file, project id). Two spellings would mean the CLI writing rows the server serving the
same directory could not see.
"""


def local_database(tasks_dir: Path) -> Path:
    """Where a directory-addressed project keeps its rows.

    The registry is what knows a project's id, and the id is what
    :meth:`~agentjobs.storage_config.StorageSettings.database_for` answers about. A
    project addressed by *directory* rather than by id -- the server's implicit
    single-project mode -- has no id to ask about, so its file is named from the
    directory instead.

    **Named from the directory, but not placed in it.** The directory is the only
    identity such a project has, so two servers started on two directories must not
    share a file -- hence the digest. Putting the file *inside* the checkout is the
    thing this migration exists to stop: it would be branch-switched, rebased over,
    and would dirty the working tree that the dispatch clean-tree gate inspects. So it
    goes beside the registry, like every other database.
    """
    resolved = Path(tasks_dir).expanduser().resolve()
    digest = hashlib.blake2s(str(resolved).encode("utf-8"), digest_size=5).hexdigest()
    return default_home() / DATABASES_DIRNAME / f"local-{resolved.name}-{digest}.db"


def open_store(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    database: Optional[Path] = None,
) -> TaskStoreBackend:
    """The task store for one project.

    ``database`` names the file, for a caller that resolved it some other way than by
    project id -- which is the implicit single-project mode and nothing else.
    """
    resolved = settings or load_storage_settings()
    if not is_server_process():
        raise StoreAccessError(
            f"Project {project.id!r} is served from SQLite, and only the AgentJobs "
            "server opens that database. Reach it over the service instead of opening "
            "storage directly -- start the server with 'agentjobs serve', or use "
            "'agentjobs storage' for the operator commands that legitimately hold the "
            "store while the server is stopped."
        )

    target = Path(database) if database is not None else resolved.database_for(project.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    store = SqlTaskStore(open_database(target), project.id)
    store.ensure_project(root=str(project.root))
    return store


def task_manager_for(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    webhook_manager: Optional[Any] = None,
) -> TaskManagerLike:
    """A manager for one project, local or remote according to who is asking.

    This is what every call site outside the API uses, and the branch left in it is
    about *processes*, not about backends:

    -   Inside the server process, a real :class:`~agentjobs.manager.TaskManager` over
        the store.
    -   Anywhere else, a :class:`~agentjobs.remote_manager.RemoteTaskManager` -- the same
        surface, over the service, because only the server opens the database.

    A caller therefore does not have to know which side it is on, which is what keeps
    the CLI's twelve construction sites from each becoming a decision.
    """
    from .manager import TaskManager

    resolved = settings or load_storage_settings()
    if not is_server_process():
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
    if is_server_process():
        return TaskManager(open_store(project, settings=resolved), webhook_manager)
    _assert_schema_current(resolved.database_for(project.id))
    with server_process():
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


def provision_project_database(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    reporting_tz: str = "UTC",
) -> Path:
    """Give a newly registered project a database of its own, and record that it has one.

    What ``agentjobs init`` calls (task-399), so a fresh install is on the database
    before its first task rather than after a migration. Returns the file.

    **The file is created here rather than left to the server's first read.** A project
    whose configuration says ``sqlite`` and whose database does not exist yet is a state
    an operator cannot tell apart from a mistake -- ``storage status`` prints
    ``(none yet)`` for it either way -- and the schema this build expects is applied on
    open, which is the moment the file is made. Doing it now means the very next command
    finds a store that is present and current.

    Opening from a short-lived process is safe *here specifically*: the file does not
    exist, so no server can be holding it. That is the same argument the cutover makes
    for its own direct open, and it is why this enters :func:`server_process` rather
    than weakening the rule that guards it.
    """
    from .storage_config import record_new_project

    resolved = settings or load_storage_settings()
    updated = record_new_project(project.id, home=resolved.home)
    target = updated.database_for(project.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with server_process():
        database = open_database(target)
        SqlTaskStore(database, project.id).ensure_project(
            root=str(project.root), reporting_tz=reporting_tz
        )
    return target


__all__ = [
    "StoreAccessError",
    "TaskStoreBackend",
    "close_databases",
    "is_server_process",
    "mark_server_process",
    "LOCAL_PROJECT_ID",
    "local_database",
    "open_database",
    "open_store",
    "provision_project_database",
    "reset_server_process",
    "server_process",
    "TaskManagerLike",
    "dispatch_manager_for",
    "task_manager_for",
]
