"""Connection ownership, pragmas and transactions for the SQLite store.

**One process opens the database, and inside it one connection writes.** That is the
owner decision recorded on task-273 (entry 6): the server is the only thing that opens
the store, and every other client reaches it over HTTP. This module is where that
single-writer rule is implemented rather than merely stated.

Readers get their own connections. WAL makes them never block the writer and never see
a half-written transaction, which is what lets the listing endpoint run while a claim is
committing.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

#: Milliseconds a blocked statement waits before raising ``SQLITE_BUSY``.
#:
#: Only relevant to a second *process* -- inside this one the write lock below
#: serialises writers before SQLite ever sees contention. It is set anyway because a
#: backup tool, a REPL or a future sidecar can legitimately open the file read-only,
#: and a bounded wait beats an instant failure.
BUSY_TIMEOUT_MS = 5000

#: ``FULL`` rather than the usual WAL recommendation of ``NORMAL``.
#:
#: ``NORMAL`` can lose the last transaction on power loss. At a few writes a minute an
#: fsync per commit is invisible, and a lost handoff is expensive -- the whole point of
#: the store is that a task record cannot silently fail to exist.
SYNCHRONOUS = "FULL"


class SqlStoreError(RuntimeError):
    """The store could not be opened or used as configured."""


def _configure(connection: sqlite3.Connection, *, read_only: bool) -> None:
    """Apply the pragmas every connection needs, in the order they must be applied."""
    connection.row_factory = sqlite3.Row
    # journal_mode is persistent, so setting it on a read-only connection would fail;
    # the writer establishes it and every later connection inherits it from the file.
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA synchronous = {SYNCHRONOUS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


class Database:
    """Owns the write connection and hands out reader connections.

    ``isolation_level=None`` throughout: Python's implicit transaction handling opens
    a *deferred* transaction on the first DML statement, which is exactly the
    upgrade-to-write pattern that produces ``SQLITE_BUSY`` deadlocks. Transactions here
    are explicit and begin ``IMMEDIATE``.
    """

    def __init__(self, path: Path) -> None:
        """Open (creating if needed) the database at ``path``."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._depth = 0
        self._writer = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        _configure(self._writer, read_only=False)
        self._readers = threading.local()

    # ----- readers -------------------------------------------------------------

    def reader(self) -> sqlite3.Connection:
        """A read connection for the calling thread, created on first use.

        Thread-local rather than pooled: a pool would need a checkout protocol at every
        call site, and one connection per reading thread is the same resource with none
        of that. They are read-only at the SQLite level, so a query that forgets to be
        a query fails loudly instead of writing outside a transaction.
        """
        existing: Optional[sqlite3.Connection] = getattr(self._readers, "connection", None)
        if existing is not None:
            return existing
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
        )
        _configure(connection, read_only=True)
        self._readers.connection = connection
        return connection

    # ----- the writer ----------------------------------------------------------

    @property
    def writer(self) -> sqlite3.Connection:
        """The single write connection.

        Exposed for schema migration and backup, which drive transaction boundaries
        themselves. Ordinary writes go through :meth:`write` and must, because that is
        what makes state, history and the outbox row commit together.
        """
        return self._writer

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Hold the write connection inside one ``IMMEDIATE`` transaction.

        Reentrant: a verb that calls another verb joins the outer transaction rather
        than opening a second one, so "state, history and the outbox row commit
        together" holds however the call was reached. Only the outermost block commits.

        ``BEGIN IMMEDIATE`` takes the write lock up front. The alternative -- SQLite's
        default deferred begin -- acquires it on the first write *after* the reads a
        decision was made on, which is the classic upgrade deadlock and also the shape
        of the read-then-decide race that ``mutate_task`` exists to prevent.
        """
        with self._write_lock:
            outermost = self._depth == 0
            if outermost:
                self._writer.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._writer
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._writer.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._writer.execute("COMMIT")

    @contextmanager
    def exclusive(self) -> Iterator[sqlite3.Connection]:
        """Hold the write connection with **no** transaction open.

        For the statements that cannot run inside one -- ``VACUUM INTO`` is the whole
        reason this exists. Taking the same lock :meth:`write` takes means the caller
        waits for a gap between transactions rather than colliding with one, which is
        what a backup taken during a write needs to do.

        Refuses rather than waits when the calling thread is *itself* inside a
        transaction: the lock is reentrant, so it would be granted, and the statement
        would then fail deep inside somebody's write with a message about vacuuming.
        """
        with self._write_lock:
            if self._depth:
                raise SqlStoreError(
                    "this operation cannot run inside a transaction; it was called "
                    "from within an open write block"
                )
            yield self._writer

    @property
    def in_transaction(self) -> bool:
        """True while a :meth:`write` block is open on this thread's behalf."""
        return self._depth > 0

    def close(self) -> None:
        """Close the writer, letting SQLite checkpoint and tidy up.

        ``PRAGMA optimize`` runs first: it updates the statistics the query planner uses
        and costs milliseconds, and skipping it is how a database gradually picks worse
        plans than the ones its indexes were measured against.
        """
        with self._write_lock:
            if self._depth:  # pragma: no cover - defensive
                raise SqlStoreError("close() called while a write transaction was open")
            try:
                self._writer.execute("PRAGMA optimize")
            finally:
                self._writer.close()
