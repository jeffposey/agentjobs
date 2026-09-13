"""A task store for a review sandbox, opened on the file its server will serve.

Every ``*_sandbox.py`` beside this file stands up a throwaway project so a change can be
looked at in a browser, and each used to say ``TaskStorage(root / "tasks")``. That
backend is gone (task-402), and what replaced it cannot be constructed from a directory
alone: a store is a database file and a project id, and the server opens a registered
project's file **by its id**.

So the id is required. Until task-427 it was optional, and with none the store fell back
to the CLI's directory-named ``_local`` file -- which every sandbox reached, because each
one seeds in ``build()`` before ``main()`` registers the project. The seed went into
``local-tasks-<hash>.db``, the server opened ``<project_id>.db``, and every review
sandbox from 2026-09-10 to 2026-09-13 served an empty project while printing seeded URLs.

The second refusal is about *where*. A sandbox is throwaway by definition, so it never
opens a file under the machine's real home: the script must point ``AGENTJOBS_HOME`` at
scratch before it imports the package, and a store asked for anywhere else is refused
rather than written to the live databases directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentjobs.projects import HOME_ENV, Project, default_home
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.storage_config import load_storage_settings
from agentjobs.store_factory import open_database, open_store, server_process

__all__ = ["SandboxStoreError", "sandbox_home", "sandbox_store"]


class SandboxStoreError(RuntimeError):
    """A sandbox asked for a store nothing would serve, or one under the real home."""


def sandbox_home() -> Path:
    """The redirected home this sandbox writes under, refusing the machine's real one."""
    if not os.environ.get(HOME_ENV):
        raise SandboxStoreError(
            f"{HOME_ENV} is not set. A sandbox must point it at a scratch directory before "
            "importing agentjobs; otherwise its seed lands in the live databases directory."
        )
    home = default_home()
    real = (Path.home() / ".agentjobs").resolve()
    if home == real:
        raise SandboxStoreError(
            f"{HOME_ENV} points at the machine's real home ({real}). A sandbox seeds "
            "throwaway records; give it a scratch directory."
        )
    return home


def sandbox_store(tasks_dir: Path, *, project_id: str) -> SqlTaskStore:
    """The store for a sandbox project, keyed on the id the script registers it under.

    A sandbox is the only writer while it seeds, and no server is up yet, so it takes
    the server declaration for the construction and drops it again -- the same argument
    ``provision_project_database`` makes for creating a file that does not exist.
    """
    if not project_id:
        raise SandboxStoreError(
            "sandbox_store needs the project id the sandbox registers: the server opens a "
            "registered project's database by id, so a seed keyed any other way is a page "
            "with nothing on it."
        )
    home = sandbox_home()
    directory = Path(tasks_dir)
    directory.mkdir(parents=True, exist_ok=True)

    database = load_storage_settings(home).database_for(project_id).resolve()
    if not database.is_relative_to(home):
        # A storage override in the calling shell wins over the home, and would win here.
        raise SandboxStoreError(
            f"The sandbox database for {project_id!r} resolves to {database}, outside the "
            f"sandbox home {home}. Unset the storage override in this shell."
        )
    database.parent.mkdir(parents=True, exist_ok=True)
    with server_process():
        store = SqlTaskStore(open_database(database), project_id)
        store.ensure_project(root=str(directory.parent))
    return store


def sandbox_project_store(project: Project) -> SqlTaskStore:
    """The store a registered sandbox project's records are in."""
    sandbox_home()
    with server_process():
        return open_store(project)
