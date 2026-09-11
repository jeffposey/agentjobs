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

**There is no longer a directory to fall back to.** It stopped being consulted at
task-402, when a project could no longer be served from files; task-380 then removed the
frozen copies themselves, so the second bullet above has happened. A store that cannot be
read returns ``None``, which a caller turns into a skip.

**And right now every caller does skip**, for a reason that has nothing to do with either
of those: see :data:`WHY_THESE_SKIP` before citing anything built on this module as a
check that runs.

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

from pathlib import Path
from typing import List, Optional

from agentjobs.models_v2 import Task
from agentjobs.projects import ProjectRegistry
from agentjobs.storage_config import load_storage_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

PROJECT_ID = "agentjobs"
"""This repository's own project id, as registered on a developer's machine."""

WHY_THESE_SKIP = """\
Every check built on this module skips, and has since task-311 -- task-411.

``tests/conftest.py`` re-points ``AGENTJOBS_HOME`` at a temp directory for **every**
test, autouse, so that nothing writes a developer's real registry. From inside a test the
machine's database therefore does not exist, ``product_tasks`` answers ``None``, and each
caller turns that into a skip. Nothing was wrong with either half; the corpus checks moved
to the store in task-311, the isolation fixture protects the registry, and the two were
never introduced to each other.

It stayed invisible because the per-file arm over the frozen records collected hundreds of
green cases beside the five skips. task-380 retired those records, which is what made the
silence audible, and measured it: turning the reads back on surfaces eighteen dangling
context pointers in the live backlog on the first run.

Fixing it here was considered and rejected. Correcting the corpus is most of the work, the
records in question belong to other sessions, and a check that reads a store several
agents are writing to would make one branch's merge depend on another agent's record being
clean at that instant. That is task-411's problem to solve deliberately, alongside
task-409, which is the same coupling seen from the gate's side.
"""
"""Why the checks in this module assert nothing at the moment. Read before trusting one."""


def product_tasks() -> Optional[List[Task]]:
    """This repository's own backlog, or ``None`` if this machine cannot read it.

    ``None`` rather than an empty list, and the difference matters: a machine where this
    repository is not registered, or where the database has been moved, must skip the
    check rather than pass it by finding nothing.

    **Under pytest it is always ``None`` today, so every caller skips** -- see
    :data:`WHY_THESE_SKIP`.
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
    """Every record this repository's checks are about.

    The same list as :func:`product_tasks` since task-380. It used to append the fixture
    records in ``tasks/test-data``, and the two names are kept apart because the callers
    mean different things by them: one is the product backlog, the other is everything
    checked.
    """
    return product_tasks()


__all__ = [
    "PROJECT_ID",
    "REPO_ROOT",
    "WHY_THESE_SKIP",
    "all_tasks",
    "product_tasks",
]
