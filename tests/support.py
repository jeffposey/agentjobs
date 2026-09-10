"""Building a task store in a test, the way the product builds one.

Most of this suite predates the database. It was written against a backend whose
constructor was a directory, so hundreds of cases said ``TaskStorage(tmp_path)`` and
meant "a store to put tasks in, isolated from every other test". That backend is gone
(task-402) and the meaning is not, so this is where it now comes from.

**A store is a file and a project id, and both are resolved the way the product resolves
them, never invented here.** A test that picked either for itself would seed a store the
server it is exercising never opens -- and the symptom is not a wrong path in a message,
it is a task that simply is not found. So:

*   Given a ``project_id``, the file comes from
    :meth:`~agentjobs.storage_config.StorageSettings.database_for`, which is what the
    server asks. Pass it whenever the test has a project id in hand: it does not depend
    on the project having been registered yet, so seeding before or after registration
    reaches the same rows.
*   Given none, the registry is asked which project contains the directory, exactly as
    ``cli._build_manager`` asks it.
*   With no registered project either, the id and the file are the ones the server's
    implicit single-project mode would use for that directory.

Nothing here declares this process the server for longer than the call: ``open_store``
refuses outside the server process, and that refusal is a real rule rather than a test
obstacle, so the declaration is scoped to the construction and dropped again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agentjobs.manager import TaskManager
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.storage_config import load_storage_settings
from agentjobs.store_factory import (
    LOCAL_PROJECT_ID,
    local_database,
    open_database,
    open_store,
    server_process,
)

__all__ = [
    "project_store",
    "quarantine_record",
    "set_updated",
    "task_manager",
    "task_store",
]


def task_store(
    tasks_dir: Path,
    *,
    project_id: Optional[str] = None,
    root: Optional[Path] = None,
) -> SqlTaskStore:
    """A store for a directory, resolved the way a command in that directory resolves."""
    directory = Path(tasks_dir)
    directory.mkdir(parents=True, exist_ok=True)
    project_root = root if root is not None else directory.parent

    if project_id == LOCAL_PROJECT_ID:
        # The implicit project is addressed by directory, not by id, so its file is
        # named from the directory -- the same rule `api.dependencies` follows. Asking
        # `database_for` would name a file the server never opens.
        database = local_database(directory)
    elif project_id is not None:
        database = load_storage_settings().database_for(project_id)
    else:
        try:
            project = ProjectRegistry().resolve_default(directory)
        except Exception:  # noqa: BLE001 - not inside a registered project
            project = None
        if project is not None:
            return project_store(project)
        project_id = LOCAL_PROJECT_ID
        database = local_database(directory)

    database.parent.mkdir(parents=True, exist_ok=True)
    store = SqlTaskStore(open_database(database), project_id)
    store.ensure_project(root=str(project_root))
    return store


def project_store(project: Project) -> SqlTaskStore:
    """The store a registered project's records are actually in."""
    with server_process():
        return open_store(project)


def task_manager(tasks_dir: Path, **kwargs: object) -> TaskManager:
    """A manager over :func:`task_store`, which is what most callers wanted."""
    return TaskManager(task_store(tasks_dir, **kwargs))  # type: ignore[arg-type]


def quarantine_record(
    store: SqlTaskStore,
    *,
    task_id: str,
    source: str,
    error: str = "expected a mapping at the top level",
    raw_text: str = "",
) -> str:
    """Record that one record could not be read, the way an import does.

    The file backend's "a broken task file must be loud, not invisible" tests used to
    write a YAML file the loader would refuse. A record that cannot satisfy the
    constraints is not a row at all now, so the state those tests were about lives in
    ``import_quarantine`` -- which is what ``load_all`` reports as a load error, and what
    every listing surface still has to show rather than silently drop.

    Returns the source name the surfaces will report it under.
    """
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO import_quarantine(project_id, source_path, task_id_guess, "
            "raw_text, error, imported_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                store.project_id,
                source,
                task_id,
                raw_text,
                error,
                datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            ),
        )
    return source


def set_updated(store: SqlTaskStore, task_id: str, when: datetime) -> None:
    """Put a value in one record's ``updated`` column, bypassing every verb.

    A verb *is* an update and stamps its own time, which is exactly what a test about
    ``updated`` having stopped mattering must not do. The file era wrote the field by
    hand into the YAML; this is the same move one layer down, and the assertion it
    serves is unchanged: whatever the column says, it does not decide the order.
    """
    with store.database.write() as connection:
        connection.execute(
            "UPDATE task SET updated_at = ? WHERE project_id = ? AND task_id = ?",
            (when.isoformat().replace("+00:00", "Z"), store.project_id, task_id),
        )
