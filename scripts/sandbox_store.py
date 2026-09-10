"""A task store for a review sandbox, resolved the way the product resolves one.

Every ``*_sandbox.py`` beside this file stands up a throwaway project so a change can be
looked at in a browser, and each used to say ``TaskStorage(root / "tasks")``. That
backend is gone (task-402), and what replaced it cannot be constructed from a directory
alone: a store is a database file and a project id, and the file is named from the id
where a project is registered and from the directory where it is not.

Resolved here rather than guessed at in nineteen scripts, and resolved the same way
``cli._build_manager`` does -- because a sandbox seeds records that a *server* then
serves, and a seed written anywhere else is a page with nothing on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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

__all__ = ["sandbox_store"]


def sandbox_store(tasks_dir: Path, *, project_id: Optional[str] = None) -> SqlTaskStore:
    """The store for a sandbox project's tasks directory.

    A sandbox is the only writer while it seeds, and no server is up yet, so it takes
    the server declaration for the construction and drops it again -- the same argument
    ``provision_project_database`` makes for creating a file that does not exist.
    """
    directory = Path(tasks_dir)
    directory.mkdir(parents=True, exist_ok=True)
    root = directory.parent

    if project_id is None:
        try:
            registered = ProjectRegistry().resolve_default(directory)
        except Exception:  # noqa: BLE001 - not inside a registered project
            registered = None
        if registered is not None:
            with server_process():
                return open_store(registered)
        project_id = LOCAL_PROJECT_ID
        database = local_database(directory)
    else:
        database = load_storage_settings().database_for(project_id)

    database.parent.mkdir(parents=True, exist_ok=True)
    with server_process():
        store = SqlTaskStore(open_database(database), project_id)
        store.ensure_project(root=str(root))
    return store


def sandbox_project_store(project: Project) -> SqlTaskStore:
    """The store a registered sandbox project's records are in."""
    with server_process():
        return open_store(project)
