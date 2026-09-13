"""Where this repository's own task records are, and how a test reads them.

The corpus checks -- every record still loads, no relationship dangles, no record quotes
a person -- are about *the backlog*, not about a directory. The backlog is rows in the
machine's store (task-311), and there is no directory to fall back to: task-402 stopped
consulting one and task-380 removed the frozen copies.

**Read from the machine's home, captured before any fixture runs** (task-411). Until then
these checks read the store through whatever ``AGENTJOBS_HOME`` said at the moment of the
read -- and ``tests/conftest.py::isolate_project_registry`` re-points that at a temp
directory for every test, autouse. From inside a test the database therefore never
existed, the read answered ``None``, every caller turned that into a skip, and the
checks asserted nothing for weeks while being cited as enforcement in three places.

So the machine's home is captured once, at import, and ``tests/conftest.py`` imports this
module before it defines a single fixture. Nothing here touches the environment, which is
what keeps the isolation fixture exactly as strong as it was: the storage configuration
and the registry are both asked for *by path*, and the database is read through a
snapshot rather than opened. Opening it would apply migrations on open
(``store_factory.open_database``), which from a branch carrying a new migration means a
test rewriting the live schema.

**A store that cannot be read fails the check rather than skipping it.** A skip is how
this went unnoticed, and the per-file arm's hundreds of green cases beside five skips
made it invisible. A machine that genuinely has no store -- a fresh clone somebody else
made -- sets :data:`OPT_OUT_ENV`, which turns the failure back into a skip whose reason
names the variable. Opting out is explicit; being opted out by accident is not possible.

**The per-file tests went with the files.** YAML stamping, loader parity and byte-level
round trips were assertions about a *file format*, parametrised over the frozen records;
with no records in the checkout they collected nothing, and a test that collects nothing
passes without asserting anything. What is left of each guarantee is named where it
lives: the stamp and the shape are a ``CHECK`` constraint the database enforces on every
write (``docs/storage-sqlite.md`` section 3), and the format an import reads and an
export writes is round-tripped over a corpus the test builds itself, in
``tests/test_storage_cli.py``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import List, Optional, Union

import pytest

from agentjobs.__version__ import __version__
from agentjobs.models_v2 import Task
from agentjobs.projects import ProjectRegistry, default_home
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.sqlstore.connection import Database
from agentjobs.sqlstore.migrations import upgrade
from agentjobs.storage_config import DATABASE_ENV, load_storage_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

PROJECT_ID = "agentjobs"
"""This repository's own project id, as registered on a developer's machine."""

MACHINE_HOME: Path = default_home()
"""The machine's AgentJobs home, as it was before any fixture moved it.

Captured at import. ``tests/conftest.py`` imports this module at the top, so the capture
precedes ``isolate_project_registry`` in every process, xdist workers included.
"""

MACHINE_DATABASE_OVERRIDE: Optional[str] = os.environ.get(DATABASE_ENV)
"""``AGENTJOBS_DATABASE`` as the suite was started with it, captured for the same reason."""

OPT_OUT_ENV = "AGENTJOBS_SKIP_CORPUS_CHECKS"
"""Set on a machine with no store for this repository, to skip the corpus checks.

Unset, a store that cannot be read is a failure. That is the whole of task-411's fix: the
old behaviour skipped, and nobody could tell a skip from a check that ran.
"""


class CorpusUnavailable(RuntimeError):
    """This machine's store for this repository could not be read."""


def snapshot(database: Path, into: Path) -> Path:
    """Copy ``database`` into ``into`` through a read-only connection.

    SQLite's online backup, so the copy is consistent while the server is writing, and the
    source is opened ``mode=ro`` so nothing on this side can write it -- not even a
    migration.
    """
    target = into / database.name
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def read_backlog(home: Path) -> List[Task]:
    """Every record of this repository in the store ``home`` configures.

    Raises :class:`CorpusUnavailable`, naming the reason, rather than answering with
    nothing: an empty list and an unreadable store must never look the same to a check.
    """
    registry = ProjectRegistry(home=home)
    try:
        registry.get(PROJECT_ID)
    except Exception as exc:  # noqa: BLE001 - any failure to resolve means not registered
        raise CorpusUnavailable(
            f"project {PROJECT_ID!r} is not registered in {registry.path}: {exc}"
        ) from exc

    # `database_for` consults the environment itself, so a test that has moved the
    # override would silently redirect the read. Refuse instead of guessing.
    if os.environ.get(DATABASE_ENV) != MACHINE_DATABASE_OVERRIDE:
        raise CorpusUnavailable(
            f"{DATABASE_ENV} has been changed since the suite started; the backlog is "
            "read from the machine's store, not from inside a test that moved it"
        )
    database = load_storage_settings(home=home).database_for(PROJECT_ID)
    if not database.is_file():
        raise CorpusUnavailable(f"the database {database} does not exist")

    with tempfile.TemporaryDirectory(
        prefix="agentjobs-corpus-", ignore_cleanup_errors=True
    ) as scratch:
        copy = Database(snapshot(database, Path(scratch)))
        try:
            upgrade(copy, agentjobs_version=__version__)
            tasks = SqlTaskStore(copy, PROJECT_ID).list_tasks()
        finally:
            copy.close()

    if not tasks:
        raise CorpusUnavailable(f"the database {database} holds no {PROJECT_ID!r} records")
    return tasks


_cached: Optional[Union[List[Task], CorpusUnavailable]] = None


def reset_cache() -> None:
    """Forget the read. For the tests that point :data:`MACHINE_HOME` elsewhere."""
    global _cached
    _cached = None


def backlog() -> List[Task]:
    """This repository's backlog, or a failed test saying why it could not be read.

    Read once per process: the store does not need to be re-read for every check, and a
    check should not see a different backlog from its neighbour because somebody filed a
    task in between.
    """
    global _cached
    if _cached is None:
        try:
            _cached = read_backlog(MACHINE_HOME)
        except CorpusUnavailable as exc:
            _cached = exc
    if isinstance(_cached, CorpusUnavailable):
        message = (
            f"this repository's own backlog could not be read: {_cached}. "
            "`agentjobs storage status` says where it is"
        )
        if os.environ.get(OPT_OUT_ENV):
            pytest.skip(f"{message} ({OPT_OUT_ENV} is set)")
        pytest.fail(
            f"{message}. On a machine with no store for this repository, set "
            f"{OPT_OUT_ENV}=1 to skip the corpus checks instead.",
            pytrace=False,
        )
    return _cached


__all__ = [
    "MACHINE_HOME",
    "OPT_OUT_ENV",
    "PROJECT_ID",
    "REPO_ROOT",
    "CorpusUnavailable",
    "backlog",
    "read_backlog",
    "reset_cache",
    "snapshot",
]
