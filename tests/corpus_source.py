"""Where this repository's own task records are.

The corpus checks -- every record still loads, no relationship dangles, no record quotes
a person -- are about *the backlog*, not about a directory. They were written when a
backlog was necessarily a directory, and they inherited two properties from that which
are not part of what they are checking:

*   **They read whatever branch is checked out.** A record fixed on ``main`` still fails
    them from a worktree whose copy predates the fix. That is the coupling task-311
    removed, and it was in the tests as well as in the product.
*   **They stop being possible once the records are retired from the checkout**, which is
    a deliberate step an operator takes after an import -- and a corpus check that
    vanishes when the corpus moves is not much of a check.

So the source is the store, and it is resolved rather than assumed: the machine's storage
configuration says which file, and the records come back through the same model whichever
way they were written.

**It never falls back to the directory** (task-402). It used to, because a project could
still be served from files; now a ``tasks/`` directory in the checkout is a frozen copy,
and answering from it would mean these checks silently validating a corpus months out of
date -- passing, and about nothing. A store that cannot be read returns ``None``, which a
caller turns into a skip.

The per-file tests -- YAML stamping, loader parity, byte-level round trips -- are
different, and stay files-only. They are assertions about a *file format*, which is what
an import reads and an export writes, and they run against a directory they make
themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Optional

import yaml

from agentjobs.models_v2 import Task, load_task
from agentjobs.projects import ProjectRegistry
from agentjobs.storage_config import load_storage_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

PROJECT_ID = "agentjobs"
"""This repository's own project id, as registered on a developer's machine."""

CORPUS_DIRS = ("tasks/agentjobs", "tasks/test-data")
"""Where the frozen copies live. ``test-data`` is fixture material that ships with the
repository and is checked alongside the product corpus."""

PRODUCT_DIR = "tasks/agentjobs"


def corpus_files() -> Iterator[Path]:
    """Every task file this repository tracks, in a stable order.

    Empty once the records have been retired from the checkout, which is what makes the
    per-file tests skip rather than fail.
    """
    for relative in CORPUS_DIRS:
        directory = REPO_ROOT / relative
        if directory.is_dir():
            yield from sorted(directory.glob("*.yaml"))


def _from_files(relative: str) -> List[Task]:
    directory = REPO_ROOT / relative
    if not directory.is_dir():
        return []
    return [
        load_task(yaml.safe_load(path.read_text(encoding="utf-8")), source=path.name)
        for path in sorted(directory.glob("*.yaml"))
    ]


def product_tasks() -> Optional[List[Task]]:
    """This repository's own backlog, or ``None`` if this machine cannot read it.

    ``None`` rather than an empty list, and the difference matters: a machine where this
    repository is not registered, or where the database has been moved, must skip the
    check rather than pass it by finding nothing.
    """
    from agentjobs.sqlstore import SqlTaskStore
    from agentjobs.store_factory import open_database, server_process

    settings = load_storage_settings()
    database = settings.database_for(PROJECT_ID)
    if not database.exists():
        return None
    try:
        ProjectRegistry().get(PROJECT_ID)
    except Exception:  # noqa: BLE001 - not registered here
        return None
    with server_process():
        found = SqlTaskStore(open_database(database), PROJECT_ID).list_tasks()
    return found or None


def all_tasks() -> Optional[List[Task]]:
    """The product backlog plus the fixture records that ship beside it."""
    product = product_tasks()
    if product is None:
        return None
    return [*product, *_from_files("tasks/test-data")]


__all__ = [
    "CORPUS_DIRS",
    "PRODUCT_DIR",
    "PROJECT_ID",
    "REPO_ROOT",
    "all_tasks",
    "corpus_files",
    "product_tasks",
]
