"""Physical schema versioning: how this store is upgraded, and how v4 would be applied.

Two version axes exist and they are deliberately independent:

* the **document** schema, ``schema: 2``, stamped on every task record and exported YAML;
* the **physical** schema, held in ``PRAGMA user_version``, which this module manages.

Coupling them was rejected on task-273: a physical migration must not invalidate every
exported document, and the two change for unrelated reasons.

Migrations are numbered ``.sql`` files applied in order, each inside one transaction,
recorded in ``schema_migration``. Forward only. A database whose ``user_version`` is
*higher* than this code knows is refused rather than opened -- an old binary meeting a
new database is the stale-server hazard this repository already documents, and the safe
response is to decline loudly.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .connection import Database, SqlStoreError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    """One numbered step, and the SQL that applies it."""

    version: int
    name: str
    path: Path

    def sql(self) -> str:
        """The statements, read at apply time rather than at import."""
        return self.path.read_text(encoding="utf-8")


def available() -> List[Migration]:
    """Every migration this code ships, in ascending version order.

    A gap or a duplicate is an error rather than something to work around: both mean
    two branches invented the same number, and applying them in file order would give
    two installations different schemas under the same ``user_version``.
    """
    found: List[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _NAME.match(path.name)
        if match is None:
            raise SqlStoreError(
                f"migration file {path.name!r} is not named NNN_lower_snake_case.sql"
            )
        found.append(Migration(int(match.group(1)), match.group(2), path))
    for index, migration in enumerate(found, start=1):
        if migration.version != index:
            raise SqlStoreError(
                f"migrations must be numbered consecutively from 001; expected "
                f"{index:03d} and found {migration.version:03d} ({migration.name})"
            )
    return found


def latest_version() -> int:
    """The physical schema version this code implements."""
    migrations = available()
    return migrations[-1].version if migrations else 0


def current_version(connection: sqlite3.Connection) -> int:
    """The physical schema version of the open database. 0 means empty."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class MigrationReport:
    """What :func:`upgrade` did, so a caller can say it rather than guess."""

    from_version: int
    to_version: int
    applied: List[str]
    snapshot: Optional[Path]

    @property
    def changed(self) -> bool:
        """True when at least one migration ran."""
        return bool(self.applied)


def upgrade(
    database: Database,
    *,
    agentjobs_version: str,
    snapshot_before: bool = True,
) -> MigrationReport:
    """Bring the database up to :func:`latest_version`, or explain why it cannot be.

    ``snapshot_before`` writes a ``VACUUM INTO`` copy beside the database before the
    first migration of a non-empty store. It is on by default because the cheapest
    moment to regret a migration is the moment before it runs, and a consistent
    snapshot of this size costs tens of milliseconds.
    """
    writer = database.writer
    have = current_version(writer)
    want = latest_version()

    if have > want:
        raise SqlStoreError(
            f"the database at {database.path} is at physical schema version {have}, but "
            f"this build of AgentJobs only knows version {want}. It was written by a "
            "newer AgentJobs and this one will not touch it: upgrade AgentJobs, or "
            "point AGENTJOBS_DATA_DIR at the database this build owns."
        )
    if have == want:
        return MigrationReport(have, want, [], None)

    pending = [m for m in available() if m.version > have]
    snapshot: Optional[Path] = None
    if snapshot_before and have > 0:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = database.path.with_name(
            f"{database.path.stem}.pre-v{want}.{stamp}{database.path.suffix}"
        )
        writer.execute("VACUUM INTO ?", (str(snapshot),))

    applied: List[str] = []
    for migration in pending:
        started = time.perf_counter()
        # One transaction per migration, including its ledger row -- a version bump
        # without the row that explains it is a database nobody can account for.
        #
        # The BEGIN rides inside the script rather than being issued before it:
        # ``executescript`` commits any *pending* transaction before it runs, so a
        # BEGIN issued out here would be thrown away. Nothing implicit happens at the
        # end, so the transaction is still open afterwards and the parameterised
        # INSERT below joins it. DDL is transactional in SQLite, so a failure at any
        # statement leaves the schema exactly as it was.
        try:
            writer.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.sql()
                + f"\nPRAGMA user_version = {migration.version};"
            )
            writer.execute(
                "INSERT INTO schema_migration(version, name, applied_at, "
                "agentjobs_version, duration_ms) VALUES (?, ?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    agentjobs_version,
                    int((time.perf_counter() - started) * 1000),
                ),
            )
        except BaseException:
            writer.execute("ROLLBACK")
            raise
        writer.execute("COMMIT")
        applied.append(f"{migration.version:03d}_{migration.name}")

    return MigrationReport(have, want, applied, snapshot)
