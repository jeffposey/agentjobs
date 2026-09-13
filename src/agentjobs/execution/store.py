"""The machine-local execution journal: one SQLite file, shared by every dispatching process.

Design section 9a (docs/agent-dispatch-design.md) is the contract; this module is its
storage half. Three things it is, and one it deliberately is not.

**It is machine-wide.** One ``execution.db`` beside the registry coordinates every project
on the machine, because the resource it rations -- ``limits.max_concurrent_runs`` -- is a
property of the machine, and admission across projects is only atomic if it is one
database. Task records stay in their own per-project stores; nothing here reads or writes
them, and nothing here is reached through ``store_factory``'s server-only rule. Three
kinds of process open this file on a normal day -- the server, ``agentjobs dispatch run``
and a scripted finish's escalation -- which is precisely the concurrency the old
directory scan could not arbitrate (task-264, P2-9).

**It is authoritative for ownership and termination.** Which attempt owns a project/task,
which slots are taken, whether a cancellation was requested and by whom, and who won the
right to write a run's terminal result. ``meta.yaml`` in a run directory is a projection
of these facts and evidence about the worker; it is never consulted to decide them,
because the worker can write it (auditor 12's question on task-264).

**It is short-transactional.** Every write is one ``BEGIN IMMEDIATE`` ... ``COMMIT``
containing no network call, no process launch and no git operation. A call that raises
committed nothing (see ``errors``). WAL with ``synchronous=FULL``: a lost terminal
transition is exactly the silent failure dispatch exists to prevent, and at a few writes
a minute an fsync per commit is invisible.

**It is not a lock on the outside world.** An ownership epoch fences *journal writes* by a
coordinator that lost ownership; it cannot prove a child process stopped. The OS task
lock and the repository merge runway stay where they are, at the git and process
boundaries (``dispatch.ledger``).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from agentjobs.execution.errors import (
    ActivityConflict,
    CapacityExhausted,
    ExecutionStoreError,
    HistoryIncompatible,
    OwnerModeConflict,
    OwnershipConflict,
    StaleOwner,
    StorageFailure,
    StoreBusy,
)

EXECUTION_DB_FILENAME = "execution.db"

SCHEMA_VERSION = 1
"""Physical schema version, in ``PRAGMA user_version``. Independent of the workflow
(reducer) version, which is stamped per execution: the table shapes and the meaning of a
history change for unrelated reasons, exactly as the task store keeps its document and
physical versions apart."""

BUSY_TIMEOUT_MS = 5000
"""How long a write waits for another process before reporting ``StoreBusy``. Every
transaction here is milliseconds long, so five seconds of contention means something is
wrong rather than busy -- and the caller is told to retry instead of being told a
transition happened."""

OWNER_LEGACY = "legacy"
OWNER_DURABLE = "durable"
OWNER_MODES = (OWNER_LEGACY, OWNER_DURABLE)

PROVENANCE_NATIVE = "native"
PROVENANCE_LEGACY = "legacy_import"

ATTEMPT_ADMITTED = "admitted"
ATTEMPT_LIVE = "live"
ATTEMPT_TERMINAL = "terminal"

SIGNAL_ENTRY_TYPES = frozenset({"handoff", "transition", "answer", "instruction"})
"""Task log entry types imported into the inbox as pending signals. Every other entry is
still recorded (as ``observed``) so the cursor can prove it saw it, but nothing waits on
a progress note."""

_SCHEMA = """
CREATE TABLE store_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE execution (
  execution_id        TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL,
  task_id             TEXT NOT NULL,
  operation_id        TEXT UNIQUE,
  workflow_version    INTEGER NOT NULL,
  envelope_json       TEXT NOT NULL CHECK (json_valid(envelope_json)),
  unknown_fields_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(unknown_fields_json)),
  provenance          TEXT NOT NULL DEFAULT 'native'
                      CHECK (provenance IN ('native', 'legacy_import')),
  source_id           TEXT UNIQUE,
  owner_mode          TEXT NOT NULL DEFAULT 'durable' CHECK (owner_mode IN ('legacy', 'durable')),
  controller          TEXT,
  controller_epoch    INTEGER NOT NULL DEFAULT 0,
  control_generation  INTEGER NOT NULL DEFAULT 0,
  state               TEXT NOT NULL,
  terminal            INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1)),
  last_sequence       INTEGER NOT NULL DEFAULT 0,
  snapshot_json       TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
  snapshot_sequence   INTEGER,
  next_due_at         TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_execution_open ON execution(project_id, task_id) WHERE terminal = 0;

CREATE TABLE execution_event (
  execution_id TEXT NOT NULL REFERENCES execution(execution_id),
  sequence     INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  observed_at  TEXT NOT NULL,
  PRIMARY KEY (execution_id, sequence),
  UNIQUE (execution_id, source_id)
);

CREATE TABLE run_attempt (
  run_id             TEXT PRIMARY KEY,
  execution_id       TEXT REFERENCES execution(execution_id),
  project_id         TEXT NOT NULL,
  task_id            TEXT NOT NULL,
  mode               TEXT NOT NULL DEFAULT '',
  takes_slot         INTEGER NOT NULL CHECK (takes_slot IN (0, 1)),
  holder             TEXT NOT NULL,
  holder_pid         INTEGER,
  epoch              INTEGER NOT NULL DEFAULT 1,
  owner_mode         TEXT NOT NULL DEFAULT 'durable' CHECK (owner_mode IN ('legacy', 'durable')),
  provenance         TEXT NOT NULL DEFAULT 'native'
                     CHECK (provenance IN ('native', 'legacy_import')),
  state              TEXT NOT NULL CHECK (state IN ('admitted', 'live', 'terminal')),
  reservation        TEXT NOT NULL DEFAULT 'held' CHECK (reservation IN ('held', 'refunded')),
  reservation_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(reservation_json)),
  cancel_requested   INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
  cancel_json        TEXT CHECK (cancel_json IS NULL OR json_valid(cancel_json)),
  control_generation INTEGER NOT NULL DEFAULT 0,
  session_id         TEXT,
  outcome            TEXT,
  status             TEXT,
  concluded_by       TEXT,
  admitted_at        TEXT NOT NULL,
  launched_at        TEXT,
  concluded_at       TEXT
);
CREATE UNIQUE INDEX ux_attempt_owner ON run_attempt(project_id, task_id) WHERE state <> 'terminal';
CREATE INDEX ix_attempt_live ON run_attempt(takes_slot) WHERE state <> 'terminal';
CREATE INDEX ix_attempt_admitted ON run_attempt(admitted_at);

CREATE TABLE activity (
  activity_id   TEXT PRIMARY KEY,
  execution_id  TEXT NOT NULL REFERENCES execution(execution_id),
  run_id        TEXT,
  kind          TEXT NOT NULL,
  input_hash    TEXT NOT NULL,
  input_json    TEXT NOT NULL CHECK (json_valid(input_json)),
  state         TEXT NOT NULL CHECK (state IN (
                  'intended', 'applied', 'not_applied', 'still_running', 'unknown', 'failed')),
  shadow        INTEGER NOT NULL DEFAULT 0 CHECK (shadow IN (0, 1)),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  owner_epoch   INTEGER NOT NULL,
  result_json   TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  error_class   TEXT,
  due_at        TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX ix_activity_open ON activity(execution_id) WHERE state IN ('intended', 'unknown', 'still_running');

CREATE TABLE source_cursor (
  source      TEXT PRIMARY KEY,
  position    INTEGER NOT NULL,
  marker      TEXT,
  updated_at  TEXT NOT NULL
);

CREATE TABLE inbox (
  inbox_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source           TEXT NOT NULL,
  source_event_id  TEXT NOT NULL,
  project_id       TEXT NOT NULL,
  task_id          TEXT NOT NULL,
  kind             TEXT NOT NULL,
  payload_json     TEXT NOT NULL CHECK (json_valid(payload_json)),
  payload_hash     TEXT NOT NULL,
  execution_id     TEXT,
  generation       INTEGER,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'observed', 'consumed', 'superseded', 'stale')),
  disposition_json TEXT CHECK (disposition_json IS NULL OR json_valid(disposition_json)),
  accepted_at      TEXT NOT NULL,
  acknowledged_at  TEXT,
  UNIQUE (source, source_event_id)
);
CREATE INDEX ix_inbox_pending ON inbox(project_id, task_id) WHERE status = 'pending';

CREATE TABLE outbox (
  operation_id    TEXT PRIMARY KEY,
  execution_id    TEXT,
  run_id          TEXT,
  project_id      TEXT NOT NULL,
  task_id         TEXT NOT NULL,
  kind            TEXT NOT NULL,
  payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'acknowledged', 'failed')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  receipt_json    TEXT CHECK (receipt_json IS NULL OR json_valid(receipt_json)),
  last_error      TEXT,
  created_at      TEXT NOT NULL,
  acknowledged_at TEXT
);
CREATE INDEX ix_outbox_pending ON outbox(created_at) WHERE status = 'pending';

CREATE TABLE child_wait (
  parent_execution_id TEXT NOT NULL REFERENCES execution(execution_id),
  child_project_id    TEXT NOT NULL,
  child_task_id       TEXT NOT NULL,
  child_execution_id  TEXT,
  attempts            INTEGER NOT NULL DEFAULT 0,
  observed_revision   TEXT,
  terminal_state      TEXT,
  grounding_cause     TEXT,
  status              TEXT NOT NULL DEFAULT 'waiting'
                      CHECK (status IN ('waiting', 'landed', 'grounded')),
  updated_at          TEXT NOT NULL,
  PRIMARY KEY (parent_execution_id, child_project_id, child_task_id)
);
"""


# ----- values handed back -----------------------------------------------------


def _loads(text: Optional[str], default: Any) -> Any:
    if text is None:
        return default
    return json.loads(text)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def digest(value: Any) -> str:
    """A stable hash of a JSON-able value: what an activity's input is compared by."""
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def this_holder() -> Tuple[str, int]:
    """Who this process is, as recorded on anything it comes to own."""
    pid = os.getpid()
    return f"{socket.gethostname()}:{pid}", pid


@dataclass(frozen=True)
class Attempt:
    """One paid agent attempt: its ownership, slot, reservation and terminal fact."""

    run_id: str
    execution_id: Optional[str]
    project_id: str
    task_id: str
    mode: str
    takes_slot: bool
    holder: str
    holder_pid: Optional[int]
    epoch: int
    owner_mode: str
    provenance: str
    state: str
    reservation: str
    reservation_data: Dict[str, Any]
    cancel_requested: bool
    cancel: Optional[Dict[str, Any]]
    control_generation: int
    session_id: Optional[str]
    outcome: Optional[str]
    status: Optional[str]
    concluded_by: Optional[str]
    admitted_at: str
    launched_at: Optional[str]
    concluded_at: Optional[str]

    @property
    def is_live(self) -> bool:
        return self.state != ATTEMPT_TERMINAL

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Attempt":
        return cls(
            run_id=row["run_id"],
            execution_id=row["execution_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            mode=row["mode"],
            takes_slot=bool(row["takes_slot"]),
            holder=row["holder"],
            holder_pid=row["holder_pid"],
            epoch=int(row["epoch"]),
            owner_mode=row["owner_mode"],
            provenance=row["provenance"],
            state=row["state"],
            reservation=row["reservation"],
            reservation_data=_loads(row["reservation_json"], {}),
            cancel_requested=bool(row["cancel_requested"]),
            cancel=_loads(row["cancel_json"], None),
            control_generation=int(row["control_generation"]),
            session_id=row["session_id"],
            outcome=row["outcome"],
            status=row["status"],
            concluded_by=row["concluded_by"],
            admitted_at=row["admitted_at"],
            launched_at=row["launched_at"],
            concluded_at=row["concluded_at"],
        )


@dataclass(frozen=True)
class Conclusion:
    """The answer to "may I write this run's terminal result?".

    ``won`` is true for exactly one caller per run, ever. The attempt is the row as it
    stands after the compare-and-set, so a loser can read who won and with what outcome.
    """

    won: bool
    attempt: Attempt


@dataclass(frozen=True)
class Execution:
    """One accepted dispatch intent."""

    execution_id: str
    project_id: str
    task_id: str
    operation_id: Optional[str]
    workflow_version: int
    envelope: Dict[str, Any]
    unknown_fields: List[str]
    provenance: str
    source_id: Optional[str]
    owner_mode: str
    controller: Optional[str]
    controller_epoch: int
    control_generation: int
    state: str
    terminal: bool
    last_sequence: int
    snapshot: Optional[Dict[str, Any]]
    snapshot_sequence: Optional[int]
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Execution":
        return cls(
            execution_id=row["execution_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            operation_id=row["operation_id"],
            workflow_version=int(row["workflow_version"]),
            envelope=_loads(row["envelope_json"], {}),
            unknown_fields=list(_loads(row["unknown_fields_json"], [])),
            provenance=row["provenance"],
            source_id=row["source_id"],
            owner_mode=row["owner_mode"],
            controller=row["controller"],
            controller_epoch=int(row["controller_epoch"]),
            control_generation=int(row["control_generation"]),
            state=row["state"],
            terminal=bool(row["terminal"]),
            last_sequence=int(row["last_sequence"]),
            snapshot=_loads(row["snapshot_json"], None),
            snapshot_sequence=row["snapshot_sequence"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class StoredEvent:
    """One event of an execution's history, as persisted."""

    sequence: int
    kind: str
    source_id: str
    payload: Dict[str, Any]
    observed_at: str


@dataclass(frozen=True)
class Activity:
    """One external effect's intent and, once known, its result."""

    activity_id: str
    execution_id: str
    run_id: Optional[str]
    kind: str
    input_hash: str
    input: Dict[str, Any]
    state: str
    shadow: bool
    attempt_count: int
    owner_epoch: int
    result: Optional[Dict[str, Any]]
    error_class: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Activity":
        return cls(
            activity_id=row["activity_id"],
            execution_id=row["execution_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            input_hash=row["input_hash"],
            input=_loads(row["input_json"], {}),
            state=row["state"],
            shadow=bool(row["shadow"]),
            attempt_count=int(row["attempt_count"]),
            owner_epoch=int(row["owner_epoch"]),
            result=_loads(row["result_json"], None),
            error_class=row["error_class"],
        )


@dataclass(frozen=True)
class OutboxItem:
    """One task-store mutation owed by the journal, keyed by its stable operation id."""

    operation_id: str
    project_id: str
    task_id: str
    kind: str
    payload: Dict[str, Any]
    execution_id: Optional[str] = None
    run_id: Optional[str] = None
    status: str = "pending"
    attempts: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxItem":
        return cls(
            operation_id=row["operation_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind=row["kind"],
            payload=_loads(row["payload_json"], {}),
            execution_id=row["execution_id"],
            run_id=row["run_id"],
            status=row["status"],
            attempts=int(row["attempts"]),
        )


@dataclass(frozen=True)
class InboxItem:
    """One imported source event."""

    inbox_id: int
    source: str
    source_event_id: str
    project_id: str
    task_id: str
    kind: str
    payload: Dict[str, Any]
    payload_hash: str
    execution_id: Optional[str]
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "InboxItem":
        return cls(
            inbox_id=int(row["inbox_id"]),
            source=row["source"],
            source_event_id=row["source_event_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind=row["kind"],
            payload=_loads(row["payload_json"], {}),
            payload_hash=row["payload_hash"],
            execution_id=row["execution_id"],
            status=row["status"],
        )


@dataclass(frozen=True)
class SourceEvent:
    """One entry of a task store's feed, in the shape the inbox imports."""

    position: int
    project_id: str
    task_id: str
    entry_id: int
    ts: str
    type: str
    actor: str
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def source_event_id(self) -> str:
        """Identity that survives a restored task database renumbering its feed."""
        return f"{self.task_id}#{self.entry_id}@{self.ts}"

    @property
    def marker(self) -> str:
        return f"{self.position}:{self.source_event_id}"


@dataclass(frozen=True)
class Cursor:
    position: int
    marker: Optional[str]


@dataclass(frozen=True)
class ChildWait:
    parent_execution_id: str
    child_project_id: str
    child_task_id: str
    child_execution_id: Optional[str]
    attempts: int
    observed_revision: Optional[str]
    terminal_state: Optional[str]
    grounding_cause: Optional[str]
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChildWait":
        return cls(
            parent_execution_id=row["parent_execution_id"],
            child_project_id=row["child_project_id"],
            child_task_id=row["child_task_id"],
            child_execution_id=row["child_execution_id"],
            attempts=int(row["attempts"]),
            observed_revision=row["observed_revision"],
            terminal_state=row["terminal_state"],
            grounding_cause=row["grounding_cause"],
            status=row["status"],
        )


# ----- error classification ---------------------------------------------------


def classify(exc: sqlite3.Error) -> ExecutionStoreError:
    """Name a SQLite failure by what a caller should do about it.

    Matched on the message because ``sqlite3`` raises one type for a dozen unrelated
    conditions, and a full disk must not be reported as "try again shortly".
    """
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return StoreBusy(f"the execution journal is busy ({exc}); nothing was committed")
    if (
        "full" in message
        or "disk i/o" in message
        or "readonly" in message
        or "read-only" in message
        or "malformed" in message
        or "not a database" in message
        or "unable to open" in message
    ):
        return StorageFailure(
            f"the execution journal could not be written ({exc}); nothing was committed"
        )
    return ExecutionStoreError(f"execution journal error ({exc}); nothing was committed")


def _event_moment(text: str) -> datetime:
    """A feed timestamp as an aware moment; an unreadable one sorts as the newest."""
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _utc(moment: Optional[datetime]) -> datetime:
    value = moment or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return _utc(moment).isoformat()


# ----- the store --------------------------------------------------------------


class ExecutionStore:
    """The journal for one machine. Safe to share between threads; one per process.

    One connection guarded by a lock, rather than a pool: every transaction is short, and
    a second connection per thread would only add ways for this process to contend with
    itself. Other *processes* open their own store on the same file, which is what WAL
    and ``BEGIN IMMEDIATE`` arbitrate.
    """

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.before_commit: Optional[Callable[[str], None]] = None
        """Called with the transaction's label immediately before ``COMMIT``.

        A fault-injection seam, and the only one: the crash-window tests raise from here
        to prove that a transition interrupted at its last instant left nothing behind.
        ``None`` everywhere in the application."""
        try:
            self._conn = sqlite3.connect(
                str(self.path),
                isolation_level=None,
                check_same_thread=False,
                timeout=busy_timeout_ms / 1000.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise classify(exc) from exc
        self.schema_version = self._ensure_schema()

    # ----- plumbing ---------------------------------------------------------

    def now(self) -> datetime:
        return _utc(self._clock())

    def close(self) -> None:
        with self._lock:
            with suppress(sqlite3.Error):
                self._conn.close()

    @property
    def compatible(self) -> bool:
        """Whether this code may mutate the file. Reads are allowed either way."""
        return self.schema_version == SCHEMA_VERSION

    def _assert_writable(self) -> None:
        if not self.compatible:
            raise HistoryIncompatible(
                f"the execution journal at {self.path} is at schema version "
                f"{self.schema_version} and this build implements {SCHEMA_VERSION}. "
                "Reads continue; every mutation is refused until a compatible AgentJobs "
                "is running."
            )

    def _ensure_schema(self) -> int:
        with self._lock:
            try:
                have = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.Error as exc:
                raise classify(exc) from exc
            if have != 0:
                return have
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise classify(exc) from exc
            try:
                # Re-read inside the write lock: a second process creating the same new
                # file must find the first one's schema rather than create it twice.
                have = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                if have == 0:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO store_meta(key, value) VALUES ('created_at', ?)",
                        (_iso(self.now()),),
                    )
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    have = SCHEMA_VERSION
                self._conn.execute("COMMIT")
            except BaseException as exc:
                with suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise classify(exc) from exc
                raise
            return have

    @contextmanager
    def transaction(self, label: str = "write") -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction. Commits on exit; raises having committed
        nothing otherwise.

        Not reentrant, deliberately: a transaction that calls a method opening another is
        a design error here (it would hold the write lock across a second decision), and
        SQLite refusing the nested ``BEGIN`` says so at once.
        """
        self._assert_writable()
        with self._lock:
            connection = self._conn
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise classify(exc) from exc
            try:
                yield connection
                if self.before_commit is not None:
                    self.before_commit(label)
                connection.execute("COMMIT")
            except BaseException as exc:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise classify(exc) from exc
                raise

    def _read(self, sql: str, parameters: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            try:
                return list(self._conn.execute(sql, parameters))
            except sqlite3.Error as exc:
                raise classify(exc) from exc

    # ----- executions and history ---------------------------------------------

    def accept_execution(
        self,
        project_id: str,
        task_id: str,
        *,
        envelope: Mapping[str, Any],
        workflow_version: int,
        operation_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        provenance: str = PROVENANCE_NATIVE,
        owner_mode: str = OWNER_DURABLE,
        unknown_fields: Sequence[str] = (),
        source_id: Optional[str] = None,
        terminal: bool = False,
        state: str = "accepted",
    ) -> Execution:
        """Commit one accepted intent, or return the one this operation already accepted.

        Idempotent on ``operation_id`` (a lost response retried) and on ``source_id`` (a
        repeated legacy import). An open execution for the same project/task under a
        different operation is an ``OwnershipConflict``: one live intent per task.
        """
        with self.transaction("accept") as connection:
            found = self._accept(
                connection,
                project_id,
                task_id,
                envelope=envelope,
                workflow_version=workflow_version,
                operation_id=operation_id,
                execution_id=execution_id,
                provenance=provenance,
                owner_mode=owner_mode,
                unknown_fields=unknown_fields,
                source_id=source_id,
                terminal=terminal,
                state=state,
            )
        return found

    def _accept(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        task_id: str,
        *,
        envelope: Mapping[str, Any],
        workflow_version: int,
        operation_id: Optional[str],
        execution_id: Optional[str],
        provenance: str,
        owner_mode: str,
        unknown_fields: Sequence[str],
        source_id: Optional[str],
        terminal: bool,
        state: str,
    ) -> Execution:
        for column, value in (("operation_id", operation_id), ("source_id", source_id)):
            if value is None:
                continue
            row = connection.execute(
                f"SELECT * FROM execution WHERE {column} = ?", (value,)
            ).fetchone()
            if row is not None:
                existing = Execution.from_row(row)
                if (existing.project_id, existing.task_id) != (project_id, task_id):
                    raise OwnershipConflict(
                        f"{column} {value!r} already accepted an execution for "
                        f"{existing.project_id}/{existing.task_id}"
                    )
                return existing
        if not terminal:
            row = connection.execute(
                "SELECT execution_id FROM execution WHERE project_id = ? AND task_id = ? "
                "AND terminal = 0",
                (project_id, task_id),
            ).fetchone()
            if row is not None:
                raise OwnershipConflict(
                    f"{project_id}/{task_id} already has open execution {row['execution_id']}"
                )
        identifier = execution_id or f"exe_{os.urandom(8).hex()}"
        now = _iso(self.now())
        connection.execute(
            "INSERT INTO execution(execution_id, project_id, task_id, operation_id, "
            "workflow_version, envelope_json, unknown_fields_json, provenance, source_id, "
            "owner_mode, state, terminal, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                project_id,
                task_id,
                operation_id,
                int(workflow_version),
                _dumps(dict(envelope)),
                _dumps(sorted(set(unknown_fields))),
                provenance,
                source_id,
                owner_mode,
                state,
                1 if terminal else 0,
                now,
                now,
            ),
        )
        self._append(
            connection,
            identifier,
            "accepted",
            {
                "envelope": dict(envelope),
                "workflow_version": int(workflow_version),
                "provenance": provenance,
                "owner_mode": owner_mode,
                "unknown_fields": sorted(set(unknown_fields)),
            },
            source_id=f"accepted:{operation_id or source_id or identifier}",
        )
        row = connection.execute(
            "SELECT * FROM execution WHERE execution_id = ?", (identifier,)
        ).fetchone()
        return Execution.from_row(row)

    def _append(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
    ) -> int:
        """Append one event inside the caller's transaction; a repeated source is a no-op."""
        existing = connection.execute(
            "SELECT sequence FROM execution_event WHERE execution_id = ? AND source_id = ?",
            (execution_id, source_id),
        ).fetchone()
        if existing is not None:
            return int(existing["sequence"])
        row = connection.execute(
            "SELECT last_sequence FROM execution WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStoreError(f"no execution {execution_id!r}")
        sequence = int(row["last_sequence"]) + 1
        now = _iso(self.now())
        connection.execute(
            "INSERT INTO execution_event(execution_id, sequence, kind, source_id, "
            "payload_json, observed_at) VALUES (?,?,?,?,?,?)",
            (execution_id, sequence, kind, source_id, _dumps(dict(payload)), now),
        )
        connection.execute(
            "UPDATE execution SET last_sequence = ?, updated_at = ? WHERE execution_id = ?",
            (sequence, now, execution_id),
        )
        return sequence

    def append_event(
        self,
        execution_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        source_id: str,
    ) -> int:
        """Append one event in its own transaction. Deduplicated on ``source_id``."""
        with self.transaction("append") as connection:
            return self._append(connection, execution_id, kind, payload, source_id=source_id)

    def execution(self, execution_id: str) -> Optional[Execution]:
        rows = self._read("SELECT * FROM execution WHERE execution_id = ?", (execution_id,))
        return Execution.from_row(rows[0]) if rows else None

    def open_execution(self, project_id: str, task_id: str) -> Optional[Execution]:
        rows = self._read(
            "SELECT * FROM execution WHERE project_id = ? AND task_id = ? AND terminal = 0",
            (project_id, task_id),
        )
        return Execution.from_row(rows[0]) if rows else None

    def executions(self, *, open_only: bool = False) -> List[Execution]:
        sql = "SELECT * FROM execution"
        if open_only:
            sql += " WHERE terminal = 0"
        return [Execution.from_row(row) for row in self._read(sql + " ORDER BY created_at")]

    def events(self, execution_id: str, *, after: int = 0) -> List[StoredEvent]:
        rows = self._read(
            "SELECT sequence, kind, source_id, payload_json, observed_at FROM execution_event "
            "WHERE execution_id = ? AND sequence > ? ORDER BY sequence",
            (execution_id, after),
        )
        return [
            StoredEvent(
                sequence=int(row["sequence"]),
                kind=row["kind"],
                source_id=row["source_id"],
                payload=_loads(row["payload_json"], {}),
                observed_at=row["observed_at"],
            )
            for row in rows
        ]

    def save_snapshot(
        self, execution_id: str, sequence: int, state: Mapping[str, Any], *, state_name: str
    ) -> None:
        """Materialise a replayed state. Accelerates replay; never replaces history."""
        with self.transaction("snapshot") as connection:
            connection.execute(
                "UPDATE execution SET snapshot_json = ?, snapshot_sequence = ?, state = ?, "
                "updated_at = ? WHERE execution_id = ? AND last_sequence >= ?",
                (
                    _dumps(dict(state)),
                    int(sequence),
                    state_name,
                    _iso(self.now()),
                    execution_id,
                    int(sequence),
                ),
            )

    def claim_controller(
        self,
        execution_id: str,
        *,
        owner_mode: str,
        holder: Optional[str] = None,
        expected_epoch: Optional[int] = None,
    ) -> int:
        """Become the controller of an execution and return the new epoch.

        Refused across owner modes: a durable controller never takes a legacy-owned
        execution, and the reverse, so an old and a new poller cannot both control one
        run. ``expected_epoch`` makes the takeover a compare-and-set.
        """
        who = holder or this_holder()[0]
        with self.transaction("controller") as connection:
            row = connection.execute(
                "SELECT owner_mode, controller_epoch, terminal FROM execution "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"no execution {execution_id!r}")
            if row["owner_mode"] != owner_mode:
                raise OwnerModeConflict(
                    f"execution {execution_id} is owned by the {row['owner_mode']} "
                    f"controller; a {owner_mode} controller may not act on it"
                )
            current = int(row["controller_epoch"])
            if expected_epoch is not None and expected_epoch != current:
                raise StaleOwner(
                    f"execution {execution_id} is at controller epoch {current}, not "
                    f"{expected_epoch}"
                )
            epoch = current + 1
            connection.execute(
                "UPDATE execution SET controller = ?, controller_epoch = ?, updated_at = ? "
                "WHERE execution_id = ?",
                (who, epoch, _iso(self.now()), execution_id),
            )
            return epoch

    def transfer_owner_mode(self, execution_id: str, *, to: str, reason: str) -> Execution:
        """Move an execution between controllers, only at a quiescent boundary.

        Quiescent means no live attempt: an attempt in flight belongs to whoever started
        it, and migrating it would give two pollers a claim on one run.
        """
        if to not in OWNER_MODES:
            raise ValueError(f"unknown owner mode {to!r}")
        with self.transaction("owner-mode") as connection:
            live = connection.execute(
                "SELECT run_id FROM run_attempt WHERE execution_id = ? AND state <> 'terminal'",
                (execution_id,),
            ).fetchone()
            if live is not None:
                raise OwnerModeConflict(
                    f"execution {execution_id} still has live attempt {live['run_id']}; "
                    "drain it before changing who controls the execution"
                )
            row = connection.execute(
                "SELECT owner_mode FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"no execution {execution_id!r}")
            if row["owner_mode"] != to:
                connection.execute(
                    "UPDATE execution SET owner_mode = ?, controller_epoch = controller_epoch + 1, "
                    "updated_at = ? WHERE execution_id = ?",
                    (to, _iso(self.now()), execution_id),
                )
                self._append(
                    connection,
                    execution_id,
                    "migrated",
                    {"from": row["owner_mode"], "to": to, "reason": reason},
                    source_id=f"migrated:{row['owner_mode']}->{to}:{os.urandom(4).hex()}",
                )
            found = connection.execute(
                "SELECT * FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return Execution.from_row(found)

    # ----- attempts: admission, ownership, termination --------------------------

    def admit(
        self,
        *,
        project_id: str,
        task_id: str,
        run_id: str,
        capacity: Optional[int],
        takes_slot: bool = True,
        mode: str = "",
        holder: Optional[str] = None,
        holder_pid: Optional[int] = None,
        envelope: Optional[Mapping[str, Any]] = None,
        workflow_version: Optional[int] = None,
        operation_id: Optional[str] = None,
        legacy_slot_holders: Iterable[str] = (),
        legacy_owners: Iterable[str] = (),
        legacy_recent_starts: Iterable[str] = (),
        hourly_limit: Optional[int] = None,
        reservation: Optional[Mapping[str, Any]] = None,
    ) -> Attempt:
        """Allocate task ownership, a machine slot and a paid-attempt reservation, together.

        One transaction, so of two processes admitting at ``capacity - 1`` exactly one
        commits and the other reads the first one's row and is refused. That is the
        primitive the ceiling never had (task-264, P2-9): the old check counted run
        directories, which are created seconds later, after the claim, ``git status`` and
        an HTTP probe.

        ``legacy_*`` are run ids the caller found in run directories that predate this
        journal. They are counted *with* the journal's rows, deduplicated by run id, so an
        upgrade under live runs neither ignores them nor counts them twice. They are read
        outside the transaction -- a directory scan inside one would hold the write lock
        across filesystem calls -- which is safe because every run started by this build
        is admitted here first, so nothing new can appear only as a directory.

        ``envelope`` accepts the execution in the same transaction (reusing an open one
        for this task); without it the attempt stands alone, which is what an interactive
        or registered session is.
        """
        who, pid = this_holder()
        who = holder or who
        pid = holder_pid if holder_pid is not None else pid
        moment = self.now()
        now = _iso(moment)
        legacy_slots = set(legacy_slot_holders)
        legacy_owners = set(legacy_owners)
        legacy_recent = set(legacy_recent_starts)
        with self.transaction("admit") as connection:
            owner = connection.execute(
                "SELECT * FROM run_attempt WHERE project_id = ? AND task_id = ? "
                "AND state <> 'terminal'",
                (project_id, task_id),
            ).fetchone()
            if owner is not None:
                raise OwnershipConflict(
                    f"{project_id}/{task_id} is owned by live attempt {owner['run_id']} "
                    f"({owner['state']})"
                )
            known = {row["run_id"] for row in connection.execute("SELECT run_id FROM run_attempt")}
            stray_owners = sorted(legacy_owners - known)
            if stray_owners:
                raise OwnershipConflict(
                    f"{project_id}/{task_id} has live run {stray_owners[0]} from before the "
                    "execution journal"
                )
            if takes_slot and capacity is not None:
                holders = [
                    row["run_id"]
                    for row in connection.execute(
                        "SELECT run_id FROM run_attempt WHERE state <> 'terminal' "
                        "AND takes_slot = 1 ORDER BY admitted_at"
                    )
                ]
                holders.extend(sorted(legacy_slots - known))
                if len(holders) >= capacity:
                    raise CapacityExhausted(
                        f"all {capacity} machine slot(s) are taken",
                        holders=tuple(holders),
                        limit="slots",
                    )
            if hourly_limit is not None:
                since = _iso(moment - timedelta(hours=1))
                started = {
                    row["run_id"]
                    for row in connection.execute(
                        "SELECT run_id FROM run_attempt WHERE admitted_at >= ? "
                        "AND reservation = 'held'",
                        (since,),
                    )
                }
                started |= legacy_recent - known
                if len(started) >= hourly_limit:
                    raise CapacityExhausted(
                        f"{len(started)} attempt(s) started in the last hour, and the cap "
                        f"is {hourly_limit}",
                        holders=tuple(sorted(started)),
                        limit="hourly",
                    )
            execution_id: Optional[str] = None
            if envelope is not None:
                open_row = connection.execute(
                    "SELECT execution_id FROM execution WHERE project_id = ? AND task_id = ? "
                    "AND terminal = 0",
                    (project_id, task_id),
                ).fetchone()
                if open_row is not None:
                    execution_id = open_row["execution_id"]
                else:
                    if workflow_version is None:
                        raise ValueError("admitting with an envelope needs a workflow_version")
                    execution_id = self._accept(
                        connection,
                        project_id,
                        task_id,
                        envelope=envelope,
                        workflow_version=workflow_version,
                        operation_id=operation_id,
                        execution_id=None,
                        provenance=PROVENANCE_NATIVE,
                        owner_mode=OWNER_DURABLE,
                        unknown_fields=(),
                        source_id=None,
                        terminal=False,
                        state="accepted",
                    ).execution_id
            try:
                connection.execute(
                    "INSERT INTO run_attempt(run_id, execution_id, project_id, task_id, mode, "
                    "takes_slot, holder, holder_pid, state, reservation_json, admitted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        execution_id,
                        project_id,
                        task_id,
                        mode,
                        1 if takes_slot else 0,
                        who,
                        pid,
                        ATTEMPT_ADMITTED,
                        _dumps(dict(reservation or {})),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OwnershipConflict(f"run {run_id} was already admitted ({exc})") from exc
            if execution_id is not None:
                attempt_no = connection.execute(
                    "SELECT COUNT(*) FROM run_attempt WHERE execution_id = ?", (execution_id,)
                ).fetchone()[0]
                self._append(
                    connection,
                    execution_id,
                    "admitted",
                    {"run_id": run_id, "attempt_no": int(attempt_no), "takes_slot": takes_slot},
                    source_id=f"admitted:{run_id}",
                )
                connection.execute(
                    "UPDATE execution SET state = 'launching' WHERE execution_id = ?",
                    (execution_id,),
                )
            row = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(row)

    def attempt(self, run_id: str) -> Optional[Attempt]:
        rows = self._read("SELECT * FROM run_attempt WHERE run_id = ?", (run_id,))
        return Attempt.from_row(rows[0]) if rows else None

    def live_attempts(
        self, *, project_id: Optional[str] = None, task_id: Optional[str] = None
    ) -> List[Attempt]:
        clauses = ["state <> 'terminal'"]
        parameters: List[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        rows = self._read(
            f"SELECT * FROM run_attempt WHERE {' AND '.join(clauses)} ORDER BY admitted_at",
            parameters,
        )
        return [Attempt.from_row(row) for row in rows]

    def mark_launched(
        self, run_id: str, *, session_id: Optional[str] = None, epoch: Optional[int] = None
    ) -> Attempt:
        """Record that the attempt's worker exists. Refused for a stale epoch."""
        with self.transaction("launched") as connection:
            row = self._attempt_for_update(connection, run_id, epoch=epoch)
            if row["state"] == ATTEMPT_TERMINAL:
                return Attempt.from_row(row)
            now = _iso(self.now())
            connection.execute(
                "UPDATE run_attempt SET state = 'live', launched_at = COALESCE(launched_at, ?), "
                "session_id = COALESCE(?, session_id) WHERE run_id = ?",
                (now, session_id, run_id),
            )
            if row["execution_id"]:
                self._append(
                    connection,
                    row["execution_id"],
                    "launched",
                    {"run_id": run_id, "session_id": session_id},
                    source_id=f"launched:{run_id}:{session_id or ''}",
                )
                connection.execute(
                    "UPDATE execution SET state = 'working' WHERE execution_id = ? "
                    "AND terminal = 0",
                    (row["execution_id"],),
                )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(found)

    def _attempt_for_update(
        self, connection: sqlite3.Connection, run_id: str, *, epoch: Optional[int]
    ) -> sqlite3.Row:
        row: Optional[sqlite3.Row] = connection.execute(
            "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStoreError(f"no attempt {run_id!r} in the execution journal")
        if epoch is not None and int(row["epoch"]) != epoch:
            raise StaleOwner(
                f"attempt {run_id} is at ownership epoch {row['epoch']}, not {epoch}; a newer "
                "owner holds it and this write was refused"
            )
        return row

    def adopt_legacy_attempt(
        self,
        *,
        run_id: str,
        project_id: str,
        task_id: str,
        mode: str = "",
        takes_slot: bool = True,
        live: bool = True,
        session_id: Optional[str] = None,
        admitted_at: Optional[str] = None,
        status: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> Attempt:
        """Give a run that predates the journal a row, owned by the legacy controller.

        Idempotent: an existing row is returned untouched, whatever it says. What the
        run directory claimed about liveness is recorded as provenance, not trusted as a
        conclusion -- a legacy row that says ``terminal`` is one whose directory already
        read terminal before the journal existed, which is the only history there is.
        """
        with self.transaction("adopt-legacy") as connection:
            row = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                return Attempt.from_row(row)
            if live:
                clash = connection.execute(
                    "SELECT run_id FROM run_attempt WHERE project_id = ? AND task_id = ? "
                    "AND state <> 'terminal'",
                    (project_id, task_id),
                ).fetchone()
                if clash is not None:
                    raise OwnershipConflict(
                        f"{project_id}/{task_id} is owned by {clash['run_id']}; legacy run "
                        f"{run_id} cannot also be live"
                    )
            self._insert_legacy_attempt(
                connection,
                run_id=run_id,
                project_id=project_id,
                task_id=task_id,
                mode=mode,
                takes_slot=takes_slot,
                live=live,
                session_id=session_id,
                admitted_at=admitted_at,
                status=status,
                outcome=outcome,
                execution_id=None,
            )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(found)

    def _insert_legacy_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        project_id: str,
        task_id: str,
        mode: str,
        takes_slot: bool,
        live: bool,
        session_id: Optional[str],
        admitted_at: Optional[str],
        status: Optional[str],
        outcome: Optional[str],
        execution_id: Optional[str],
    ) -> None:
        state = ATTEMPT_LIVE if live else ATTEMPT_TERMINAL
        connection.execute(
            "INSERT INTO run_attempt(run_id, execution_id, project_id, task_id, mode, takes_slot, "
            "holder, owner_mode, provenance, state, session_id, status, outcome, admitted_at, "
            "launched_at, concluded_at, concluded_by) VALUES "
            "(?,?,?,?,?,?,'legacy','legacy','legacy_import',?,?,?,?,?,?,?,?)",
            (
                run_id,
                execution_id,
                project_id,
                task_id,
                mode,
                1 if takes_slot else 0,
                state,
                session_id,
                None if live else status,
                None if live else outcome,
                admitted_at or _iso(self.now()),
                admitted_at,
                None if live else _iso(self.now()),
                None if live else "legacy_import",
            ),
        )

    def import_legacy_run(
        self,
        *,
        run_id: str,
        project_id: str,
        task_id: str,
        mode: str,
        takes_slot: bool,
        live: bool,
        session_id: Optional[str],
        admitted_at: Optional[str],
        status: Optional[str],
        outcome: Optional[str],
        envelope: Mapping[str, Any],
        unknown_fields: Sequence[str],
        workflow_version: int,
    ) -> Tuple[str, Optional[Attempt]]:
        """Import one pre-journal run as a legacy execution. One transaction; repeatable.

        Returns ``(disposition, attempt)``: ``imported``; ``already`` when a previous
        import (or a native admission) owns the run; or ``conflict`` when the run's
        directory says live but another open execution already owns its task -- two live
        owners of one task is exactly what migration must never create, so it reports
        rather than chooses.

        The execution is ``legacy_import`` provenance and ``legacy`` owner mode, and every
        envelope field the run directory did not record is listed as unknown rather than
        filled in from today's defaults.
        """
        source_id = f"legacy-run:{run_id}"
        with self.transaction("legacy-import") as connection:
            row = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None and row["execution_id"]:
                return "already", Attempt.from_row(row)
            if row is not None and row["provenance"] != PROVENANCE_LEGACY:
                return "already", Attempt.from_row(row)
            if live:
                clash = connection.execute(
                    "SELECT execution_id FROM execution WHERE project_id = ? AND task_id = ? "
                    "AND terminal = 0 AND (source_id IS NULL OR source_id <> ?)",
                    (project_id, task_id, source_id),
                ).fetchone()
                if clash is not None:
                    return "conflict", Attempt.from_row(row) if row is not None else None
            execution = self._accept(
                connection,
                project_id,
                task_id,
                envelope=envelope,
                workflow_version=workflow_version,
                operation_id=None,
                execution_id=None,
                provenance=PROVENANCE_LEGACY,
                owner_mode=OWNER_LEGACY,
                unknown_fields=unknown_fields,
                source_id=source_id,
                terminal=not live,
                state="working"
                if live
                else ("cancelled" if outcome == "cancelled" else "concluded"),
            )
            if row is None:
                self._insert_legacy_attempt(
                    connection,
                    run_id=run_id,
                    project_id=project_id,
                    task_id=task_id,
                    mode=mode,
                    takes_slot=takes_slot,
                    live=live,
                    session_id=session_id,
                    admitted_at=admitted_at,
                    status=status,
                    outcome=outcome,
                    execution_id=execution.execution_id,
                )
            else:
                connection.execute(
                    "UPDATE run_attempt SET execution_id = ? WHERE run_id = ?",
                    (execution.execution_id, run_id),
                )
            self._append(
                connection,
                execution.execution_id,
                "admitted",
                {"run_id": run_id, "attempt_no": 1, "takes_slot": takes_slot},
                source_id=f"admitted:{run_id}",
            )
            if session_id:
                self._append(
                    connection,
                    execution.execution_id,
                    "launched",
                    {"run_id": run_id, "session_id": session_id},
                    source_id=f"launched:{run_id}:{session_id}",
                )
            if not live:
                self._append(
                    connection,
                    execution.execution_id,
                    "concluded",
                    {
                        "run_id": run_id,
                        "outcome": outcome or "",
                        "status": status or "",
                        "by": "legacy_import",
                    },
                    source_id=f"concluded:{run_id}",
                )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return "imported", Attempt.from_row(found)

    def request_cancel(
        self,
        run_id: str,
        *,
        requester: str,
        source: str,
        reason: str = "",
    ) -> Attempt:
        """Record who asked for a run to stop, before anything is signalled.

        Bumps the control generation, so a result computed by a coordinator that read the
        attempt before the request is refused when it tries to conclude against the old
        generation. Idempotent in effect: a second request records its own requester and
        leaves the run cancelled-requested.
        """
        with self.transaction("cancel") as connection:
            row = self._attempt_for_update(connection, run_id, epoch=None)
            if row["state"] == ATTEMPT_TERMINAL:
                return Attempt.from_row(row)
            request = {
                "requester": requester,
                "source": source,
                "reason": reason,
                "requested_at": _iso(self.now()),
            }
            connection.execute(
                "UPDATE run_attempt SET cancel_requested = 1, cancel_json = ?, "
                "control_generation = control_generation + 1 WHERE run_id = ?",
                (_dumps(request), run_id),
            )
            if row["execution_id"]:
                generation = int(row["control_generation"]) + 1
                self._append(
                    connection,
                    row["execution_id"],
                    "stop_requested",
                    {"run_id": run_id, "generation": generation, **request},
                    source_id=f"stop:{run_id}:{generation}",
                )
                connection.execute(
                    "UPDATE execution SET control_generation = control_generation + 1, "
                    "state = 'stopping' WHERE execution_id = ? AND terminal = 0",
                    (row["execution_id"],),
                )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(found)

    def conclude(
        self,
        run_id: str,
        *,
        outcome: str,
        status: str,
        concluded_by: str,
        epoch: Optional[int] = None,
        expected_generation: Optional[int] = None,
        refund: bool = False,
        projection: Optional[OutboxItem] = None,
    ) -> Conclusion:
        """The one compare-and-set terminal transition for a run.

        Exactly one caller per run gets ``won=True``; it alone writes the run's terminal
        ``dispatch_result`` and its terminal meta. A poller and a cancellation racing each
        other both call this, and the loser learns the winner's outcome instead of writing
        a contradictory one (task-107's ``cancelled`` then ``interrupted``).

        ``expected_generation`` refuses a conclusion decided before a cancellation was
        requested: the concluder read generation N, a Stop moved it to N+1, and the poll
        result it computed is no longer the decision to publish. ``projection`` is the
        task-store write owed by the winner, enqueued in the same transaction so a crash
        after this commit still leaves the terminal result to be delivered.

        Ownership and the slot are released by this same transaction, because they are
        the same fact: an attempt that is over holds nothing.
        """
        with self.transaction("conclude") as connection:
            row = self._attempt_for_update(connection, run_id, epoch=epoch)
            if row["state"] == ATTEMPT_TERMINAL:
                return Conclusion(False, Attempt.from_row(row))
            if expected_generation is not None and int(row["control_generation"]) != int(
                expected_generation
            ):
                return Conclusion(False, Attempt.from_row(row))
            now = _iso(self.now())
            cursor = connection.execute(
                "UPDATE run_attempt SET state = 'terminal', outcome = ?, status = ?, "
                "concluded_by = ?, concluded_at = ?, "
                "reservation = CASE WHEN ? THEN 'refunded' ELSE reservation END "
                "WHERE run_id = ? AND state <> 'terminal'",
                (outcome, status, concluded_by, now, 1 if refund else 0, run_id),
            )
            won = cursor.rowcount == 1
            if won and row["execution_id"]:
                self._append(
                    connection,
                    row["execution_id"],
                    "concluded",
                    {"run_id": run_id, "outcome": outcome, "status": status, "by": concluded_by},
                    source_id=f"concluded:{run_id}",
                )
                final = "cancelled" if outcome == "cancelled" else "concluded"
                connection.execute(
                    "UPDATE execution SET state = ?, terminal = 1, updated_at = ? "
                    "WHERE execution_id = ?",
                    (final, now, row["execution_id"]),
                )
            if won and projection is not None:
                self._enqueue(connection, projection)
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Conclusion(won, Attempt.from_row(found))

    def take_over(
        self, run_id: str, *, holder: str, expected_epoch: int, owner_mode: str
    ) -> Attempt:
        """Fence the previous owner out of this attempt's journal writes.

        It does not stop the previous owner's process -- nothing written here can -- so
        a caller uses this only after establishing that process cannot continue.
        """
        with self.transaction("take-over") as connection:
            row = self._attempt_for_update(connection, run_id, epoch=expected_epoch)
            if row["owner_mode"] != owner_mode:
                raise OwnerModeConflict(
                    f"attempt {run_id} is owned by the {row['owner_mode']} controller"
                )
            connection.execute(
                "UPDATE run_attempt SET holder = ?, epoch = epoch + 1 WHERE run_id = ?",
                (holder, run_id),
            )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(found)

    # ----- activities -----------------------------------------------------------

    def record_intent(
        self,
        activity_id: str,
        *,
        execution_id: str,
        kind: str,
        input: Mapping[str, Any],
        owner_epoch: int,
        run_id: Optional[str] = None,
        shadow: bool = False,
    ) -> Tuple[Activity, bool]:
        """Persist an effect's intent before the effect. Returns ``(activity, created)``.

        Replaying the same history names the same activity with the same input, which is
        a no-op; the same id with a different input is refused, because a stable id that
        can mean two effects deduplicates nothing.
        """
        payload = dict(input)
        hashed = digest(payload)
        with self.transaction("intent") as connection:
            row = connection.execute(
                "SELECT * FROM activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if row is not None:
                if row["input_hash"] != hashed:
                    raise ActivityConflict(
                        f"activity {activity_id} was recorded with a different input"
                    )
                return Activity.from_row(row), False
            now = _iso(self.now())
            connection.execute(
                "INSERT INTO activity(activity_id, execution_id, run_id, kind, input_hash, "
                "input_json, state, shadow, owner_epoch, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,'intended',?,?,?,?)",
                (
                    activity_id,
                    execution_id,
                    run_id,
                    kind,
                    hashed,
                    _dumps(payload),
                    1 if shadow else 0,
                    int(owner_epoch),
                    now,
                    now,
                ),
            )
            found = connection.execute(
                "SELECT * FROM activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            return Activity.from_row(found), True

    def record_result(
        self,
        activity_id: str,
        *,
        state: str,
        owner_epoch: int,
        result: Optional[Mapping[str, Any]] = None,
        error_class: Optional[str] = None,
    ) -> Activity:
        """Record what an effect did, only if the recording controller still owns it."""
        with self.transaction("result") as connection:
            row = connection.execute(
                "SELECT a.*, e.controller_epoch FROM activity a JOIN execution e "
                "ON e.execution_id = a.execution_id WHERE a.activity_id = ?",
                (activity_id,),
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"no activity {activity_id!r}")
            if int(row["controller_epoch"]) != int(owner_epoch):
                raise StaleOwner(
                    f"activity {activity_id} belongs to controller epoch "
                    f"{row['controller_epoch']}; a result from epoch {owner_epoch} is refused"
                )
            connection.execute(
                "UPDATE activity SET state = ?, result_json = ?, error_class = ?, "
                "attempt_count = attempt_count + 1, owner_epoch = ?, updated_at = ? "
                "WHERE activity_id = ?",
                (
                    state,
                    _dumps(dict(result)) if result is not None else None,
                    error_class,
                    int(owner_epoch),
                    _iso(self.now()),
                    activity_id,
                ),
            )
            found = connection.execute(
                "SELECT * FROM activity WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            return Activity.from_row(found)

    def activities(self, execution_id: Optional[str] = None) -> List[Activity]:
        if execution_id is None:
            rows = self._read("SELECT * FROM activity ORDER BY created_at, activity_id")
        else:
            rows = self._read(
                "SELECT * FROM activity WHERE execution_id = ? ORDER BY created_at, activity_id",
                (execution_id,),
            )
        return [Activity.from_row(row) for row in rows]

    def in_flight_activities(self) -> List[Activity]:
        """Effects whose intent is recorded and whose result is not known."""
        rows = self._read(
            "SELECT * FROM activity WHERE state IN ('intended', 'unknown', 'still_running') "
            "AND shadow = 0 ORDER BY created_at"
        )
        return [Activity.from_row(row) for row in rows]

    # ----- inbox ----------------------------------------------------------------

    def cursor(self, source: str) -> Cursor:
        rows = self._read("SELECT position, marker FROM source_cursor WHERE source = ?", (source,))
        if not rows:
            return Cursor(0, None)
        return Cursor(int(rows[0]["position"]), rows[0]["marker"])

    def reset_cursor(self, source: str, *, reason: str) -> None:
        """Rewind a source to its beginning. Deduplication makes the re-import safe."""
        del reason
        with self.transaction("cursor-reset") as connection:
            connection.execute("DELETE FROM source_cursor WHERE source = ?", (source,))

    def import_source_events(
        self,
        source: str,
        events: Sequence[SourceEvent],
        *,
        not_before: Optional[datetime] = None,
    ) -> int:
        """Insert a batch into the inbox and advance the cursor, in one transaction.

        The cursor moves only here, and only together with the rows it moved past, so a
        crash between reading the feed and this commit re-reads the same batch next time
        and a crash after it has nothing left to lose. Returns how many were new.

        ``not_before`` skips entries older than a moment while still moving the cursor
        past them: history from before any execution this journal holds is owed to
        nobody, and copying a project's whole log into the inbox on first contact would
        be volume without a reader.
        """
        if not events:
            return 0
        ordered = sorted(events, key=lambda event: event.position)
        inserted = 0
        with self.transaction("import") as connection:
            now = _iso(self.now())
            for event in ordered:
                if not_before is not None and _event_moment(event.ts) < not_before:
                    continue
                payload = {
                    "entry_id": event.entry_id,
                    "type": event.type,
                    "actor": event.actor,
                    "ts": event.ts,
                    "data": event.data,
                }
                open_row = connection.execute(
                    "SELECT execution_id, control_generation FROM execution "
                    "WHERE project_id = ? AND task_id = ? AND terminal = 0",
                    (event.project_id, event.task_id),
                ).fetchone()
                status = "pending" if event.type in SIGNAL_ENTRY_TYPES else "observed"
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO inbox(source, source_event_id, project_id, task_id, "
                    "kind, payload_json, payload_hash, execution_id, generation, status, "
                    "accepted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source,
                        event.source_event_id,
                        event.project_id,
                        event.task_id,
                        event.type,
                        _dumps(payload),
                        digest(payload),
                        open_row["execution_id"] if open_row else None,
                        int(open_row["control_generation"]) if open_row else None,
                        status,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    if open_row is not None and status == "pending":
                        self._append(
                            connection,
                            open_row["execution_id"],
                            "signal",
                            {"source_event_id": event.source_event_id, "kind": event.type},
                            source_id=f"signal:{source}:{event.source_event_id}",
                        )
            last = ordered[-1]
            connection.execute(
                "INSERT INTO source_cursor(source, position, marker, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET position = excluded.position, "
                "marker = excluded.marker, updated_at = excluded.updated_at "
                "WHERE excluded.position > source_cursor.position",
                (source, last.position, last.marker, now),
            )
        return inserted

    def inbox(
        self, *, project_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[InboxItem]:
        clauses: List[str] = []
        parameters: List[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._read(f"SELECT * FROM inbox{where} ORDER BY inbox_id", parameters)
        return [InboxItem.from_row(row) for row in rows]

    # ----- outbox ---------------------------------------------------------------

    def _enqueue(self, connection: sqlite3.Connection, item: OutboxItem) -> None:
        existing = connection.execute(
            "SELECT payload_json FROM outbox WHERE operation_id = ?", (item.operation_id,)
        ).fetchone()
        if existing is not None:
            if _loads(existing["payload_json"], {}) != item.payload:
                raise ActivityConflict(
                    f"outbox operation {item.operation_id} was enqueued with a different payload"
                )
            return
        connection.execute(
            "INSERT INTO outbox(operation_id, execution_id, run_id, project_id, task_id, kind, "
            "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                item.operation_id,
                item.execution_id,
                item.run_id,
                item.project_id,
                item.task_id,
                item.kind,
                _dumps(item.payload),
                _iso(self.now()),
            ),
        )

    def enqueue(self, item: OutboxItem) -> None:
        with self.transaction("enqueue") as connection:
            self._enqueue(connection, item)

    def pending_outbox(self, *, project_id: Optional[str] = None) -> List[OutboxItem]:
        if project_id is None:
            rows = self._read("SELECT * FROM outbox WHERE status = 'pending' ORDER BY created_at")
        else:
            rows = self._read(
                "SELECT * FROM outbox WHERE status = 'pending' AND project_id = ? "
                "ORDER BY created_at",
                (project_id,),
            )
        return [OutboxItem.from_row(row) for row in rows]

    def outbox_item(self, operation_id: str) -> Optional[OutboxItem]:
        rows = self._read("SELECT * FROM outbox WHERE operation_id = ?", (operation_id,))
        return OutboxItem.from_row(rows[0]) if rows else None

    def acknowledge(self, operation_id: str, *, receipt: Mapping[str, Any]) -> None:
        with self.transaction("acknowledge") as connection:
            connection.execute(
                "UPDATE outbox SET status = 'acknowledged', receipt_json = ?, "
                "attempts = attempts + 1, acknowledged_at = ? WHERE operation_id = ? "
                "AND status = 'pending'",
                (_dumps(dict(receipt)), _iso(self.now()), operation_id),
            )

    def record_outbox_failure(self, operation_id: str, *, error: str, final: bool) -> None:
        with self.transaction("outbox-failure") as connection:
            connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, "
                "status = CASE WHEN ? THEN 'failed' ELSE status END WHERE operation_id = ?",
                (error[:1000], 1 if final else 0, operation_id),
            )

    # ----- child waits ----------------------------------------------------------

    def add_child_wait(
        self, parent_execution_id: str, *, project_id: str, task_id: str
    ) -> ChildWait:
        """One durable subscription per child; a repeated add returns the existing one."""
        with self.transaction("child-wait") as connection:
            connection.execute(
                "INSERT OR IGNORE INTO child_wait(parent_execution_id, child_project_id, "
                "child_task_id, updated_at) VALUES (?,?,?,?)",
                (parent_execution_id, project_id, task_id, _iso(self.now())),
            )
            row = connection.execute(
                "SELECT * FROM child_wait WHERE parent_execution_id = ? AND child_project_id = ? "
                "AND child_task_id = ?",
                (parent_execution_id, project_id, task_id),
            ).fetchone()
            return ChildWait.from_row(row)

    def observe_child(
        self,
        parent_execution_id: str,
        *,
        project_id: str,
        task_id: str,
        revision: str,
        terminal_state: Optional[str] = None,
        grounding_cause: Optional[str] = None,
        child_execution_id: Optional[str] = None,
        attempted: bool = False,
    ) -> ChildWait:
        """Record what a child's record said. Grounding is sticky: a resumed supervisor
        cannot forget a child that failed."""
        with self.transaction("child-observe") as connection:
            row = connection.execute(
                "SELECT * FROM child_wait WHERE parent_execution_id = ? AND child_project_id = ? "
                "AND child_task_id = ?",
                (parent_execution_id, project_id, task_id),
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(
                    f"no wait on {project_id}/{task_id} under {parent_execution_id}"
                )
            status = row["status"]
            if status != "grounded":
                if grounding_cause:
                    status = "grounded"
                elif terminal_state:
                    status = "landed"
            connection.execute(
                "UPDATE child_wait SET observed_revision = ?, "
                "terminal_state = COALESCE(?, terminal_state), "
                "grounding_cause = COALESCE(grounding_cause, ?), status = ?, "
                "child_execution_id = COALESCE(?, child_execution_id), "
                "attempts = attempts + ?, updated_at = ? "
                "WHERE parent_execution_id = ? AND child_project_id = ? AND child_task_id = ?",
                (
                    revision,
                    terminal_state,
                    grounding_cause,
                    status,
                    child_execution_id,
                    1 if attempted else 0,
                    _iso(self.now()),
                    parent_execution_id,
                    project_id,
                    task_id,
                ),
            )
            found = connection.execute(
                "SELECT * FROM child_wait WHERE parent_execution_id = ? AND child_project_id = ? "
                "AND child_task_id = ?",
                (parent_execution_id, project_id, task_id),
            ).fetchone()
            return ChildWait.from_row(found)

    def child_waits(self, parent_execution_id: str) -> List[ChildWait]:
        rows = self._read(
            "SELECT * FROM child_wait WHERE parent_execution_id = ? "
            "ORDER BY child_project_id, child_task_id",
            (parent_execution_id,),
        )
        return [ChildWait.from_row(row) for row in rows]

    # ----- backup and restore ---------------------------------------------------

    def backup(self, destination: Path) -> Path:
        """A consistent snapshot through SQLite's online backup API.

        The supported way to copy a live WAL database: a file copy can capture the main
        file without the WAL frames that have not been checkpointed into it yet.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            destination_connection = sqlite3.connect(str(target))
            try:
                self._conn.backup(destination_connection)
            except sqlite3.Error as exc:
                raise classify(exc) from exc
            finally:
                destination_connection.close()
        return target


def restore_snapshot(snapshot: Path, target: Path) -> ExecutionStore:
    """Replace the journal at ``target`` with ``snapshot``'s contents, and open it.

    An operator act, with every dispatching process stopped. The restored journal
    describes the past; it must be reconciled against the world
    (``coordinator.reconcile_after_restore``) before anything acts on it -- restoring an
    old database is not undoing a launch, a merge or a task write.
    """
    source = sqlite3.connect(f"file:{Path(snapshot)}?mode=ro", uri=True)
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    except sqlite3.Error as exc:
        raise classify(exc) from exc
    finally:
        source.close()
    return ExecutionStore(target)


__all__ = [
    "ATTEMPT_ADMITTED",
    "ATTEMPT_LIVE",
    "ATTEMPT_TERMINAL",
    "Activity",
    "Attempt",
    "ChildWait",
    "Conclusion",
    "Cursor",
    "EXECUTION_DB_FILENAME",
    "Execution",
    "ExecutionStore",
    "InboxItem",
    "OWNER_DURABLE",
    "OWNER_LEGACY",
    "OutboxItem",
    "PROVENANCE_LEGACY",
    "PROVENANCE_NATIVE",
    "SCHEMA_VERSION",
    "SourceEvent",
    "StoredEvent",
    "classify",
    "digest",
    "restore_snapshot",
    "this_holder",
]
