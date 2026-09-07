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
from typing import Dict, Iterator, Optional, Union

from .__version__ import __version__
from .projects import Project
from .sqlstore import SqlTaskStore
from .sqlstore.connection import Database
from .sqlstore.migrations import upgrade
from .storage import TaskStorage
from .storage_config import StorageSettings, load_storage_settings

TaskStoreBackend = Union[TaskStorage, SqlTaskStore]


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
]
