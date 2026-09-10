"""Moving one project's records out of a shared database into a file of its own.

The cutover moves a project from YAML files into a database. This module moves it from
*somebody else's* database into its own, which is the second half of the same idea: a
project is the unit everything in AgentJobs is scoped to, and storage was the one place
where projects shared a container (task-400).

## The sequence, and why it is that order

1.  **Back up the source.** The same ``VACUUM INTO`` snapshot the cutover takes, and for
    the same reason: the step after it removes rows, and a removal is only recoverable
    if something was written down first.
2.  **Create the destination and bring its schema up.** Empty, and at the same physical
    version as the source, so the copy is a row move rather than a migration.
3.  **Copy, in one transaction.** ``ATTACH`` plus one ``INSERT ... SELECT`` per
    project-scoped table. An interruption leaves the destination without a commit and
    the source untouched.
4.  **Verify field by field.** Every task in the source is compared against the row it
    became, exactly as the cutover's verification does and with the same function, plus
    a row count per table and the backlog invariant. A count agreeing with a count is
    not verification.
5.  **Record.** ``storage.yaml`` gains the project's ``database``. It is written before
    the source rows go, deliberately: a crash after this points the project at a file
    that holds its records, while a crash after a delete-first would point it at a file
    that no longer does.
6.  **Delete the source rows.** Only now, and only on a verification that passed.

## What is copied, and how the list is decided

Every table carrying a ``project_id`` column, discovered from the schema rather than
listed here. A hand-written list is a list somebody has to remember to extend, and the
failure when they do not is that a future table's rows are silently left behind in the
shared file -- exactly the silent, total failure this whole design is trying to remove.
Discovery also excludes the fts5 shadow tables for free, none of which has that column.

``blob`` is the exception and is not project-scoped: it is content addressed, so the
same image attached in two projects is one row. Only the blobs this project's
attachments reference are copied, and only the ones nothing else references any more are
removed from the source.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .__version__ import __version__
from .cutover import _document as comparable_document
from .cutover import _first_difference as first_difference
from .projects import Project
from .sqlstore import SqlTaskStore
from .sqlstore.connection import Database
from .sqlstore.migrations import upgrade
from .storage_config import (
    StorageSettings,
    default_project_database,
    load_storage_settings,
    record_database,
)
from .store_factory import open_database, server_process

#: Copied first, so the rows a foreign key points at exist before the rows that point at
#: them. ``defer_foreign_keys`` makes this unnecessary for correctness and it is kept
#: anyway: an ordering that reads correctly is cheaper to trust than a pragma somebody
#: has to remember is in force.
PARENTS_FIRST = ("project", "task", "log_entry", "attachment")

#: The attached schema name. Local to one transaction, so it cannot collide.
ATTACHED = "split"


class SplitError(RuntimeError):
    """The move could not be made, and nothing was changed."""


# ----- what the schema says is project-scoped -------------------------------


def _columns(connection: sqlite3.Connection, table: str) -> List[str]:
    """The insertable columns of ``table``.

    ``table_info`` rather than ``table_xinfo`` because it omits STORED generated columns
    -- ``task.priority_rank`` and ``task_event.open_delta`` -- which an ``INSERT`` must
    not name and which SQLite recomputes in the destination anyway.
    """
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def project_tables(connection: sqlite3.Connection) -> List[str]:
    """Every table a project's rows are in, parents first.

    Decided by looking for a ``project_id`` column rather than by a list in this file,
    so a table added by a later migration is copied without anybody remembering to come
    back here.
    """
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    scoped = [name for name in names if "project_id" in _columns(connection, name)]

    def rank(name: str) -> Tuple[int, str]:
        return (PARENTS_FIRST.index(name) if name in PARENTS_FIRST else len(PARENTS_FIRST), name)

    return sorted(scoped, key=rank)


# ----- verification ---------------------------------------------------------


@dataclass
class SplitVerification:
    """Whether the destination holds what the source held, unchanged.

    Field-level, like the cutover's. The row counts are here as well because a task
    document compares equal while its history does not: ``task_event`` and ``task_run``
    are not part of a task's document and are exactly the rows a naive copy loses.
    """

    tasks: int = 0
    copied_tasks: int = 0
    missing: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    differing: List[Tuple[str, str]] = field(default_factory=list)
    counts: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    blobs_expected: int = 0
    blobs_present: int = 0
    open_delta: int = 0
    open_rows: int = 0

    @property
    def short_counts(self) -> List[str]:
        """Tables whose destination count does not match the source."""
        return [table for table, (source, dest) in sorted(self.counts.items()) if source != dest]

    @property
    def ok(self) -> bool:
        """True when the destination may be made authoritative and the source cleared."""
        return (
            not self.missing
            and not self.extra
            and not self.differing
            and not self.short_counts
            and self.blobs_expected == self.blobs_present
            and self.open_delta == self.open_rows
        )

    def render(self) -> str:
        """An operator-readable summary, naming everything that did not match."""
        lines = [
            f"{self.copied_tasks} task(s) copied against {self.tasks} in the source",
            f"backlog invariant: sum(open_delta)={self.open_delta} "
            f"open_tasks={self.open_rows} "
            f"{'OK' if self.open_delta == self.open_rows else 'MISMATCH'}",
            f"attachments: {self.blobs_present}/{self.blobs_expected} blobs present",
            "rows: "
            + ", ".join(f"{table}={dest}" for table, (_, dest) in sorted(self.counts.items())),
        ]
        if self.missing:
            lines.append(f"MISSING from the new file: {', '.join(sorted(self.missing))}")
        if self.extra:
            lines.append(f"in the new file but not the source: {', '.join(sorted(self.extra))}")
        if self.differing:
            lines.append(f"{len(self.differing)} record(s) differ from the source:")
            lines.extend(f"  {task_id}: {detail}" for task_id, detail in self.differing)
        for table in self.short_counts:
            source, dest = self.counts[table]
            lines.append(f"{table}: {source} row(s) in the source, {dest} copied")
        lines.append("VERIFIED" if self.ok else "NOT VERIFIED")
        return "\n".join(lines)


def verify_split(
    source: SqlTaskStore,
    destination: SqlTaskStore,
    *,
    counts: Dict[str, Tuple[int, int]],
) -> SplitVerification:
    """Compare the two stores task by task, and the tables row for row.

    ``comparable_document`` is the cutover's own comparison, imported rather than
    reimplemented: it normalises a timestamp to an instant and drops derived fields, and
    two verifications that disagree about what "the same record" means would be worse
    than one of them not existing.
    """
    report = SplitVerification(counts=dict(counts))
    on_source = {task.id: comparable_document(task) for task in source.list_tasks()}
    in_destination = {task.id: comparable_document(task) for task in destination.list_tasks()}
    report.tasks = len(on_source)
    report.copied_tasks = len(in_destination)
    report.missing = [task_id for task_id in on_source if task_id not in in_destination]
    report.extra = [task_id for task_id in in_destination if task_id not in on_source]
    for task_id, document in on_source.items():
        stored = in_destination.get(task_id)
        if stored is not None and stored != document:
            report.differing.append((task_id, first_difference(document, stored)))

    for task in destination.list_tasks():
        for entry in task.log:
            for attachment in entry.attachments or []:
                report.blobs_expected += 1
                if destination.attachments.has(attachment.sha256):
                    report.blobs_present += 1

    report.open_delta, report.open_rows = destination.open_delta_reconciles()
    return report


# ----- the move -------------------------------------------------------------


@dataclass
class SplitReport:
    """Everything one split produced, for a caller that has to print it."""

    project_id: str
    source: Path
    destination: Path
    verified: SplitVerification
    backup: Optional[Path] = None
    copied: Dict[str, int] = field(default_factory=dict)
    blobs_copied: int = 0
    removed: Dict[str, int] = field(default_factory=dict)
    recorded: bool = False

    @property
    def ok(self) -> bool:
        """True when the project is now served from its own file."""
        return self.recorded and self.verified.ok

    def render(self) -> str:
        """An operator-readable summary of what moved."""
        lines = [f"{self.project_id}: {self.source} -> {self.destination}"]
        if self.backup:
            lines.append(f"backed up to {self.backup}")
        lines.append(
            f"copied {self.copied.get('task', 0)} task(s), "
            f"{self.copied.get('task_event', 0)} history event(s), "
            f"{self.blobs_copied} attachment blob(s)"
        )
        lines.append(self.verified.render())
        if self.removed:
            lines.append(f"removed {self.removed.get('task', 0)} task row(s) from {self.source}")
        return "\n".join(lines)


def _counts_for(
    connection: sqlite3.Connection, schema: str, project_id: str, tables: Sequence[str]
) -> Dict[str, int]:
    """Row counts for this project in one attached schema."""
    counted: Dict[str, int] = {}
    for table in tables:
        row = connection.execute(
            f'SELECT COUNT(*) FROM {schema}."{table}" WHERE project_id = ?', (project_id,)
        ).fetchone()
        counted[table] = int(row[0])
    return counted


def _copy(
    connection: sqlite3.Connection, project_id: str, tables: Sequence[str]
) -> Tuple[Dict[str, int], int]:
    """Copy this project's rows, and the blobs its attachments reference."""
    connection.execute("PRAGMA defer_foreign_keys = ON")
    blobs = connection.execute(
        f"INSERT OR IGNORE INTO {ATTACHED}.blob (sha256, media_type, size_bytes, content) "
        "SELECT sha256, media_type, size_bytes, content FROM main.blob "
        "WHERE sha256 IN (SELECT sha256 FROM main.attachment WHERE project_id = ?)",
        (project_id,),
    ).rowcount
    for table in tables:
        columns = ", ".join(f'"{name}"' for name in _columns(connection, table))
        connection.execute(
            f'INSERT INTO {ATTACHED}."{table}" ({columns}) '
            f'SELECT {columns} FROM main."{table}" WHERE project_id = ?',
            (project_id,),
        )
    return _counts_for(connection, ATTACHED, project_id, tables), max(blobs, 0)


def _remove(
    connection: sqlite3.Connection, project_id: str, tables: Sequence[str]
) -> Dict[str, int]:
    """Take this project's rows out of the source, children before parents.

    The orphaned blobs go last and by refcount rather than by project: a blob attached
    in two projects is one row, and deleting it because one of them left would corrupt
    the other's attachment.
    """
    connection.execute("PRAGMA defer_foreign_keys = ON")
    removed = _counts_for(connection, "main", project_id, tables)
    for table in reversed(list(tables)):
        connection.execute(f'DELETE FROM main."{table}" WHERE project_id = ?', (project_id,))
    connection.execute("DELETE FROM main.blob WHERE sha256 NOT IN (SELECT sha256 FROM attachment)")
    return removed


def split_project(
    project: Project,
    *,
    settings: Optional[StorageSettings] = None,
    destination: Optional[Path] = None,
    skip_backup: bool = False,
) -> SplitReport:
    """Give one project a database of its own, taking its rows out of the shared one.

    Refuses rather than guesses on every precondition it can check: a project not served
    from a database, a source that does not exist, a destination that does, and a
    destination that is already where the project's records are.

    The source rows are removed only after the copy verifies **and** the configuration
    records the new location. A failed verification leaves both files intact and the
    project still served from the shared one, which is the state an operator can
    inspect with ordinary queries.
    """
    from .cutover import back_up

    resolved = settings or load_storage_settings()
    source = resolved.database_for(project.id)
    target = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else default_project_database(project.id, resolved.home)
    )
    if not source.exists():
        raise SplitError(
            f"Project {project.id!r} is configured to be served from {source}, and that "
            "file does not exist. Nothing was changed."
        )
    if target == source:
        raise SplitError(
            f"Project {project.id!r} is already served from {source}. Name a different "
            "file with --into, or leave it where it is."
        )
    if target.exists():
        raise SplitError(
            f"{target} already exists. This command never writes into a database it did "
            "not create: look at what is there, then either remove it deliberately or "
            "pass --into with another path."
        )

    backup = None if skip_backup else back_up(resolved, database=source)

    created = Database(target)
    try:
        upgrade(created, agentjobs_version=__version__)
    finally:
        created.close()

    with server_process():
        database = open_database(source)
        with database.exclusive() as connection:
            connection.execute(f"ATTACH DATABASE ? AS {ATTACHED}", (str(target),))
        try:
            with database.write() as connection:
                tables = project_tables(connection)
                source_counts = _counts_for(connection, "main", project.id, tables)
                copied, blobs = _copy(connection, project.id, tables)
        finally:
            with database.exclusive() as connection:
                connection.execute(f"DETACH DATABASE {ATTACHED}")

        moved = open_database(target)
        verified = verify_split(
            SqlTaskStore(database, project.id),
            SqlTaskStore(moved, project.id),
            counts={table: (source_counts[table], copied[table]) for table in tables},
        )

        report = SplitReport(
            project_id=project.id,
            source=source,
            destination=target,
            verified=verified,
            backup=backup,
            copied=copied,
            blobs_copied=blobs,
        )
        if not verified.ok:
            return report

        record_database(project.id, target, home=resolved.home)
        report.recorded = True

        with database.write() as connection:
            report.removed = _remove(connection, project.id, tables)
    return report


__all__ = [
    "ATTACHED",
    "PARENTS_FIRST",
    "SplitError",
    "SplitReport",
    "SplitVerification",
    "project_tables",
    "split_project",
    "verify_split",
]
