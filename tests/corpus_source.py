"""Where this repository's own task records are, whichever backend holds them.

The corpus checks -- every record still loads, no relationship dangles, no record quotes
a person -- are about *the backlog*, not about a directory. They were written when a
backlog was necessarily a directory, and they inherited two properties from that which
are not part of what they are checking:

*   **They read whatever branch is checked out.** A record fixed on ``main`` still fails
    them from a worktree whose copy predates the fix. That is the coupling task-311
    removes, and it was in the tests as well as in the product.
*   **They stop being possible once the records are retired from the checkout**, which is
    a deliberate step an operator takes after a cutover -- and a corpus check that
    vanishes when the corpus moves is not much of a check.

So the source is resolved rather than assumed. A project on files answers from the files;
a project on the database answers from the rows, through the same model. The checks
themselves do not change, which is the point: they are about the records.

The per-file tests -- YAML stamping, loader parity, byte-level round trips -- are
different, and stay files-only. They are assertions about a *file format*, and skipping
them where there are no files is the honest answer rather than a gap.
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
"""Where the records live while they are files. ``test-data`` is fixture material that
ships with the repository and is checked alongside the product corpus."""

PRODUCT_DIR = "tasks/agentjobs"


def on_sqlite() -> bool:
    """True when this machine serves this repository's own records from the database.

    Read from the machine's storage configuration rather than inferred from whether a
    directory happens to exist: an operator who has cut over but not yet retired the
    files has both, and the configuration is the thing that decides which one is true.
    """
    try:
        return load_storage_settings().on_sqlite(PROJECT_ID)
    except Exception:  # noqa: BLE001 - an unreadable config is not this test's subject
        return False


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


def _from_store() -> Optional[List[Task]]:
    """Every record the database holds for this project, or ``None`` if it cannot answer.

    ``None`` rather than an empty list, and the difference matters: a machine where this
    repository is not registered, or where the database has been moved, must skip the
    check rather than pass it by finding nothing.
    """
    from agentjobs.sqlstore import SqlTaskStore
    from agentjobs.store_factory import open_database, server_process

    settings = load_storage_settings()
    if not settings.database.exists():
        return None
    try:
        ProjectRegistry().get(PROJECT_ID)
    except Exception:  # noqa: BLE001 - not registered here
        return None
    with server_process():
        return SqlTaskStore(open_database(settings.database), PROJECT_ID).list_tasks()


def product_tasks() -> Optional[List[Task]]:
    """This repository's own backlog, from whichever backend holds it.

    ``None`` when it cannot be read at all, which a caller turns into a skip.
    """
    if on_sqlite():
        return _from_store()
    records = _from_files(PRODUCT_DIR)
    return records or None


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
    "on_sqlite",
    "product_tasks",
]
