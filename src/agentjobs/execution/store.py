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
import uuid
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

from agentjobs.dispatch.pids import process_created_after
from agentjobs.execution.errors import (
    ActivityConflict,
    AlreadyQueued,
    CapacityExhausted,
    ExecutionStoreError,
    HistoryIncompatible,
    OwnerModeConflict,
    OwnershipConflict,
    QueueFull,
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

SCHEMA_REVISION = 7
"""Additive revisions applied on top of physical schema version 1 (task-416).

Revision 3 (task-417) adds the auth-recovery incident, waiter and probe tables, which no
earlier build reads. Revision 4 (task-447) adds ``idle_session_event``, the record of every
session the idle sweep stopped and every change of its enforcement mode. Revision 5
(task-459) adds ``dispatch_queue``, the machine's durable queue of authorised dispatches
waiting for a slot -- a table the previous build neither reads nor writes, so a queue
filled by the new server is simply invisible to an older process rather than misread.
Revision 6 (task-462) adds ``pull_arming``, the per-project record of a person having
switched the pull mode on with a bound -- the sole authority for a pulled dispatch, and
again a table the previous build neither reads nor writes.

Revision 7 (task-482) adds ``run_attempt.slot_released_at`` and ``slot_released_reason``:
when an attempt gave its machine slot back while still live. The previous build does not
read them and so keeps counting such an attempt -- it refuses a dispatch it could have
allowed, which is the safe direction for an old process to be wrong in.

**Deliberately not a ``user_version`` bump.** Processes running the previous build share
this file with the new one -- an epic walk started before an upgrade keeps dispatching
its children from the old code for hours -- and a bumped version makes every one of
those refuse every mutation (``compatible`` is false), which grounds the walk on the
upgrade itself. Revision 2 only adds tables and nullable columns the old build never
reads, so the old build stays correct beside it. A change an old build could misread
still bumps ``SCHEMA_VERSION`` and fails closed, as before."""

_ADDITIONS = """
CREATE TABLE IF NOT EXISTS timer (
  timer_id      TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  execution_id  TEXT,
  kind          TEXT NOT NULL,
  due_at        TEXT NOT NULL,
  payload_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  created_at    TEXT NOT NULL,
  fired_at      TEXT,
  cancelled_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_timer_due ON timer(due_at)
  WHERE fired_at IS NULL AND cancelled_at IS NULL;

CREATE TABLE IF NOT EXISTS supervision (
  walk_id          TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL,
  parent_task_id   TEXT NOT NULL,
  authority_entry  INTEGER NOT NULL,
  authority_actor  TEXT NOT NULL,
  settings_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),
  state            TEXT NOT NULL CHECK (state IN ('walking', 'done', 'stopped')),
  grounding_json   TEXT CHECK (grounding_json IS NULL OR json_valid(grounding_json)),
  stop             TEXT,
  detail           TEXT,
  host             TEXT NOT NULL DEFAULT 'process',
  holder           TEXT,
  holder_pid       INTEGER,
  epoch            INTEGER NOT NULL DEFAULT 0,
  started          INTEGER NOT NULL DEFAULT 0,
  peak_in_flight   INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_supervision_open
  ON supervision(project_id, parent_task_id) WHERE state = 'walking';

CREATE TABLE IF NOT EXISTS supervision_child (
  walk_id            TEXT NOT NULL REFERENCES supervision(walk_id),
  child_task_id      TEXT NOT NULL,
  seq                INTEGER NOT NULL,
  status             TEXT NOT NULL CHECK (status IN (
                       'admitting', 'flying', 'landed', 'retry_owed', 'grounded')),
  attempts_reserved  INTEGER NOT NULL DEFAULT 0,
  operation_id       TEXT,
  run_id             TEXT,
  execution_id       TEXT,
  history_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(history_json)),
  deadline_at        TEXT,
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (walk_id, child_task_id)
);

CREATE TABLE IF NOT EXISTS auth_incident (
  incident_id    TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  profile_key    TEXT NOT NULL,
  profile_json   TEXT NOT NULL CHECK (json_valid(profile_json)),
  state          TEXT NOT NULL CHECK (state IN ('open', 'recovered', 'closed')),
  opened_at      TEXT NOT NULL,
  notify_at      TEXT,
  notified_at    TEXT,
  next_probe_at  TEXT NOT NULL,
  resets_at      TEXT,
  last_result_json TEXT CHECK (last_result_json IS NULL OR json_valid(last_result_json)),
  closed_reason  TEXT,
  updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_auth_incident_open
  ON auth_incident(kind, profile_key) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS auth_waiter (
  incident_id    TEXT NOT NULL REFERENCES auth_incident(incident_id),
  run_id         TEXT NOT NULL,
  project_id     TEXT NOT NULL,
  task_id        TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  stall_at       TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN (
                   'waiting', 'nudging', 'nudged', 'recovered', 'removed', 'uncertain', 'escalated')),
  nudges         INTEGER NOT NULL DEFAULT 0,
  detail_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
  joined_at      TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (incident_id, run_id)
);
CREATE INDEX IF NOT EXISTS ix_auth_waiter_run ON auth_waiter(run_id);

CREATE TABLE IF NOT EXISTS auth_probe (
  probe_id       TEXT PRIMARY KEY,
  incident_id    TEXT NOT NULL REFERENCES auth_incident(incident_id),
  profile_key    TEXT NOT NULL,
  holder         TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  result_json    TEXT CHECK (result_json IS NULL OR json_valid(result_json))
);
CREATE INDEX IF NOT EXISTS ix_auth_probe_profile ON auth_probe(profile_key, started_at);

CREATE TABLE IF NOT EXISTS idle_session_event (
  event_id       TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('stop', 'mode')),
  at             TEXT NOT NULL,
  session_id     TEXT,
  outcome        TEXT NOT NULL,
  detail_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))
);
CREATE INDEX IF NOT EXISTS ix_idle_session_event_at ON idle_session_event(kind, at);

CREATE TABLE IF NOT EXISTS dispatch_queue (
  queue_id     TEXT PRIMARY KEY,
  seq          INTEGER NOT NULL UNIQUE,
  project_id   TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'manual',
  request_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(request_json)),
  queued_by    TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'starting', 'started', 'cancelled', 'refused')),
  run_id       TEXT,
  detail       TEXT NOT NULL DEFAULT '',
  attempts     INTEGER NOT NULL DEFAULT 0,
  queued_at    TEXT NOT NULL,
  claimed_at   TEXT,
  settled_at   TEXT,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_queue_task
  ON dispatch_queue(project_id, task_id) WHERE status IN ('queued', 'starting');
CREATE INDEX IF NOT EXISTS ix_dispatch_queue_waiting
  ON dispatch_queue(seq) WHERE status IN ('queued', 'starting');

CREATE TABLE IF NOT EXISTS pull_arming (
  arming_id     TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL,
  armed_by      TEXT NOT NULL,
  armed_at      TEXT NOT NULL,
  bound_kind    TEXT NOT NULL CHECK (bound_kind IN ('starts', 'until', 'open')),
  bound_starts  INTEGER,
  bound_until   TEXT,
  posture       TEXT,
  state         TEXT NOT NULL DEFAULT 'armed'
                CHECK (state IN ('armed', 'disarmed', 'spent', 'expired', 'faulted')),
  started       INTEGER NOT NULL DEFAULT 0,
  failures      INTEGER NOT NULL DEFAULT 0,
  detail        TEXT NOT NULL DEFAULT '',
  retired_at    TEXT,
  retired_by    TEXT,
  updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pull_arming_open
  ON pull_arming(project_id) WHERE state = 'armed';
CREATE INDEX IF NOT EXISTS ix_pull_arming_project ON pull_arming(project_id, armed_at);
"""

_ADDED_COLUMNS = (
    ("run_attempt", "operation_id", "TEXT"),
    ("execution", "controlled_by", "TEXT"),
    ("run_attempt", "slot_released_at", "TEXT"),
    ("run_attempt", "slot_released_reason", "TEXT"),
)
"""``run_attempt.operation_id`` is the admission's stable operation id, so a supervisor
that died between a child's admission and its own bookkeeping finds the same attempt
rather than admitting a second one. ``execution.controlled_by`` is ``controller`` for an
execution the durable controller drives and ``NULL`` for everything else -- including
every row the previous build writes, which is what keeps the legacy poller in charge of
them. ``run_attempt.slot_released_at`` and ``slot_released_reason`` are task-482's: when
and why a live attempt stopped counting against the machine ceiling, ``NULL`` for every
attempt that still counts."""

CONTROLLED_BY_CONTROLLER = "controller"


# ----- values handed back -----------------------------------------------------


def _column(row: sqlite3.Row, name: str) -> Any:
    """A column that revision 2 added, read as ``None`` from a row that predates it."""
    return row[name] if name in row.keys() else None


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


# ``process_created_after`` moved to ``dispatch.pids`` with task-505. It was task-444's
# answer to a recycled pid and the only one in the codebase; the dispatch subsystem had
# the same question in five more places and was answering it with a bare liveness probe.


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
    operation_id: Optional[str] = None
    slot_released_at: Optional[str] = None
    """When this attempt gave its machine slot back while still live (task-482).

    ``None`` for every attempt that still counts, which is nearly all of them. It is set
    once and never cleared: a run that has released its slot does not take one again, and
    a release that could be undone would be a slot two dispatches could both be told is
    free."""
    slot_released_reason: str = ""
    """Why, in one machine-readable word. ``task_closed`` is the only one today."""

    @property
    def is_live(self) -> bool:
        return self.state != ATTEMPT_TERMINAL

    @property
    def holds_slot(self) -> bool:
        """Whether this attempt is one of the machine's occupied slots *now*.

        ``takes_slot`` is the admission's fact -- what this kind of run costs the machine
        -- and stays true for the life of the row. This is the live question, and the two
        differ for exactly as long as a session outlives the task it was dispatched for.
        Every count of the ceiling asks this one.
        """
        return self.takes_slot and self.slot_released_at is None

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
            operation_id=_column(row, "operation_id"),
            slot_released_at=_column(row, "slot_released_at"),
            slot_released_reason=_column(row, "slot_released_reason") or "",
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
    controlled_by: Optional[str] = None
    next_due_at: Optional[str] = None

    @property
    def controller_driven(self) -> bool:
        """Whether the durable controller, rather than the legacy poller, drives it."""
        return self.controlled_by == CONTROLLED_BY_CONTROLLER

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
            controlled_by=_column(row, "controlled_by"),
            next_due_at=row["next_due_at"],
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


@dataclass(frozen=True)
class Timer:
    """One persisted due time."""

    timer_id: str
    owner: str
    execution_id: Optional[str]
    kind: str
    due_at: str
    payload: Dict[str, Any]
    fired_at: Optional[str]
    cancelled_at: Optional[str]

    @property
    def pending(self) -> bool:
        return self.fired_at is None and self.cancelled_at is None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Timer":
        return cls(
            timer_id=row["timer_id"],
            owner=row["owner"],
            execution_id=row["execution_id"],
            kind=row["kind"],
            due_at=row["due_at"],
            payload=_loads(row["payload_json"], {}),
            fired_at=row["fired_at"],
            cancelled_at=row["cancelled_at"],
        )


QUEUE_WAITING = "queued"
QUEUE_STARTING = "starting"
QUEUE_STARTED = "started"
QUEUE_CANCELLED = "cancelled"
QUEUE_REFUSED = "refused"

QUEUE_OPEN_STATUSES = (QUEUE_WAITING, QUEUE_STARTING)
"""The two statuses that still hold a place in line. Everything else is history."""


@dataclass(frozen=True)
class QueuedDispatch:
    """One authorised dispatch waiting for a machine slot (task-459).

    Everything a run would carry, minus the run: the task, the project, how the
    authorisation is to be established at start time, who asked and when. It is not an
    execution and has no history -- an execution begins at admission, and this row exists
    precisely because admission has not happened yet.
    """

    queue_id: str
    seq: int
    project_id: str
    task_id: str
    source: str
    request: Dict[str, Any]
    queued_by: str
    status: str
    run_id: Optional[str]
    detail: str
    attempts: int
    queued_at: str
    claimed_at: Optional[str]
    settled_at: Optional[str]
    updated_at: str

    @property
    def waiting(self) -> bool:
        return self.status in QUEUE_OPEN_STATUSES

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueuedDispatch":
        return cls(
            queue_id=row["queue_id"],
            seq=int(row["seq"]),
            project_id=row["project_id"],
            task_id=row["task_id"],
            source=row["source"],
            request=_loads(row["request_json"], {}),
            queued_by=row["queued_by"] or "",
            status=row["status"],
            run_id=row["run_id"],
            detail=row["detail"] or "",
            attempts=int(row["attempts"] or 0),
            queued_at=row["queued_at"],
            claimed_at=row["claimed_at"],
            settled_at=row["settled_at"],
            updated_at=row["updated_at"],
        )


PULL_ARMED = "armed"
PULL_DISARMED = "disarmed"
PULL_SPENT = "spent"
PULL_EXPIRED = "expired"
PULL_FAULTED = "faulted"

PULL_RETIRED_STATES = (PULL_DISARMED, PULL_SPENT, PULL_EXPIRED, PULL_FAULTED)
"""Every way an arming ends. Four rather than one, because the sentence a person reads
on the board -- *you turned it off* against *it ran out* against *it kept failing* -- is
the whole of what they need, and a single ``closed`` with a detail string would put that
distinction somewhere only a careful reader finds it."""

BOUND_STARTS = "starts"
BOUND_UNTIL = "until"
BOUND_OPEN = "open"


@dataclass(frozen=True)
class PullArming:
    """One person having switched the pull mode on for one project, with a bound (task-462).

    **The sole authority for a pulled dispatch.** Everything a later start has to be able
    to establish without asking anybody is here: who armed it, when, how much of the
    bound is left, and the posture they chose. It is read at every tick and never taken
    from a request, which is what makes a pulled run's authorising entry evidence rather
    than a claim -- see :mod:`agentjobs.dispatch.pull`.

    It is not an execution and holds no run. A run it starts has its own execution, its
    own envelope and its own record; this row only ever says that a person authorised
    starting them.
    """

    arming_id: str
    project_id: str
    armed_by: str
    armed_at: str
    bound_kind: str
    bound_starts: Optional[int]
    bound_until: Optional[str]
    posture: Optional[str]
    state: str
    started: int
    failures: int
    detail: str
    retired_at: Optional[str]
    retired_by: Optional[str]
    updated_at: str

    @property
    def armed(self) -> bool:
        return self.state == PULL_ARMED

    @property
    def starts_left(self) -> Optional[int]:
        """How many more runs this arming may buy, or ``None`` when it is not counted."""
        if self.bound_kind != BOUND_STARTS or self.bound_starts is None:
            return None
        return max(0, self.bound_starts - self.started)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PullArming":
        return cls(
            arming_id=row["arming_id"],
            project_id=row["project_id"],
            armed_by=row["armed_by"],
            armed_at=row["armed_at"],
            bound_kind=row["bound_kind"],
            bound_starts=(None if row["bound_starts"] is None else int(row["bound_starts"])),
            bound_until=row["bound_until"],
            posture=row["posture"],
            state=row["state"],
            started=int(row["started"] or 0),
            failures=int(row["failures"] or 0),
            detail=row["detail"] or "",
            retired_at=row["retired_at"],
            retired_by=row["retired_by"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class Supervision:
    """One durable epic walk: the authority it runs on and where it has got to."""

    walk_id: str
    project_id: str
    parent_task_id: str
    authority_entry: int
    authority_actor: str
    settings: Dict[str, Any]
    state: str
    grounding: Optional[Dict[str, Any]]
    stop: Optional[str]
    detail: Optional[str]
    host: str
    holder: Optional[str]
    holder_pid: Optional[int]
    epoch: int
    started: int
    peak_in_flight: int
    updated_at: str = ""
    """When the holder last wrote to this walk.

    Read back so a reader outside the walk can tell a live supervisor from a pid the
    operating system has since handed to something else -- the same question
    :meth:`open_walk` settles with :func:`process_created_after`, asked by anything that
    has to know whether a walk is still flying."""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Supervision":
        return cls(
            walk_id=row["walk_id"],
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            authority_entry=int(row["authority_entry"]),
            authority_actor=row["authority_actor"],
            settings=_loads(row["settings_json"], {}),
            state=row["state"],
            grounding=_loads(row["grounding_json"], None),
            stop=row["stop"],
            detail=row["detail"],
            host=row["host"],
            holder=row["holder"],
            holder_pid=row["holder_pid"],
            epoch=int(row["epoch"]),
            started=int(row["started"]),
            peak_in_flight=int(row["peak_in_flight"]),
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class SupervisedChild:
    """One child of a durable walk: its admissions, attempts and what became of them."""

    walk_id: str
    child_task_id: str
    seq: int
    status: str
    attempts_reserved: int
    operation_id: Optional[str]
    run_id: Optional[str]
    execution_id: Optional[str]
    history: List[Dict[str, Any]]
    deadline_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SupervisedChild":
        return cls(
            walk_id=row["walk_id"],
            child_task_id=row["child_task_id"],
            seq=int(row["seq"]),
            status=row["status"],
            attempts_reserved=int(row["attempts_reserved"]),
            operation_id=row["operation_id"],
            run_id=row["run_id"],
            execution_id=row["execution_id"],
            history=list(_loads(row["history_json"], [])),
            deadline_at=row["deadline_at"],
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


def _parse_moment(text: Optional[str]) -> Optional[datetime]:
    """A stored ISO stamp as an aware moment, or ``None`` when it cannot be read.

    ``None`` on an unreadable stamp rather than a guess, because the one caller
    (:meth:`ExecutionStore.claim_queued_dispatch`) treats it as "old enough to reclaim" --
    and a row whose claim time is unreadable is a row no living process is describing.
    """
    if not text:
        return None
    try:
        return _utc(datetime.fromisoformat(str(text).replace("Z", "+00:00")))
    except ValueError:
        return None


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
                if have == SCHEMA_VERSION:
                    self._ensure_additions()
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
            self._ensure_additions()
            return have

    def _ensure_additions(self) -> None:
        """Apply ``SCHEMA_REVISION``'s additive tables and columns, once per file.

        Checked with a read first, so a store opened on an up-to-date file takes no write
        lock at all; applied under ``BEGIN IMMEDIATE`` with a re-read, so two processes
        upgrading the same file at once apply it once.
        """

        def revision() -> int:
            row = self._conn.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_revision'"
            ).fetchone()
            return int(row[0]) if row is not None else 1

        try:
            if revision() >= SCHEMA_REVISION:
                return
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise classify(exc) from exc
        try:
            if revision() < SCHEMA_REVISION:
                for statement in _ADDITIONS.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
                for table, column, kind in _ADDED_COLUMNS:
                    present = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                    if column not in present:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_attempt_operation "
                    "ON run_attempt(operation_id) WHERE operation_id IS NOT NULL"
                )
                self._conn.execute(
                    "INSERT INTO store_meta(key, value) VALUES ('schema_revision', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_REVISION),),
                )
            self._conn.execute("COMMIT")
        except BaseException as exc:
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise classify(exc) from exc
            raise

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

    def read(self, sql: str, parameters: Sequence[Any] = ()) -> List[sqlite3.Row]:
        """A read outside any transaction, for a module that owns its own tables here.

        ``dispatch.auth_recovery`` (task-417) and ``dispatch.idle_sessions`` (task-447) are
        the callers. Their tables live in this file so their transitions share the journal's
        WAL and busy contract, but their logic is not the journal's and does not belong in
        this class.
        """
        return self._read(sql, parameters)

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
        controlled_by: Optional[str] = None,
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
                controlled_by=controlled_by,
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
        controlled_by: Optional[str] = None,
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
            "owner_mode, state, terminal, created_at, updated_at, controlled_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                controlled_by,
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
                "controlled_by": controlled_by,
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

    def latest_execution(self, project_id: str, task_id: str) -> Optional[Execution]:
        """The newest execution accepted for this project/task, open or terminal.

        Newest by acceptance, with the insertion order breaking a tie between two rows
        stamped in the same instant. What a continuation reads its envelope from
        (task-375).
        """
        rows = self._read(
            "SELECT * FROM execution WHERE project_id = ? AND task_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
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
        attempt_operation_id: Optional[str] = None,
        continues_execution_id: Optional[str] = None,
        controlled_by: Optional[str] = None,
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

        **An open execution is continued only when this admission names it**
        (``continues_execution_id``, task-416). An execution stays open after a retryable
        attempt ends so its controller can run the next attempt under the same grant; any
        other admission for the task -- a person's new dispatch, an interactive claim, a
        registered session -- is a new act, and it supersedes that execution in this same
        transaction rather than quietly joining a grant it never made.

        ``attempt_operation_id`` makes the admission itself idempotent: a caller that died
        between committing it and recording the run id it got retries with the same id and
        is handed the attempt it already has, whatever state that attempt is in now.
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
            if attempt_operation_id is not None:
                already = connection.execute(
                    "SELECT * FROM run_attempt WHERE operation_id = ?", (attempt_operation_id,)
                ).fetchone()
                if already is not None:
                    if (already["project_id"], already["task_id"]) != (project_id, task_id):
                        raise OwnershipConflict(
                            f"admission {attempt_operation_id} already admitted "
                            f"{already['project_id']}/{already['task_id']}"
                        )
                    return Attempt.from_row(already)
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
                    # `slot_released_at IS NULL`, not `takes_slot = 1` alone: a session
                    # whose task has closed gave its slot back and is still live
                    # (task-482). Counting it here is what let a finished run block the
                    # next dispatch for as long as the person kept talking to it.
                    for row in connection.execute(
                        "SELECT run_id FROM run_attempt WHERE state <> 'terminal' "
                        "AND takes_slot = 1 AND slot_released_at IS NULL "
                        "ORDER BY admitted_at"
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
            open_row = connection.execute(
                "SELECT execution_id FROM execution WHERE project_id = ? AND task_id = ? "
                "AND terminal = 0",
                (project_id, task_id),
            ).fetchone()
            if open_row is not None and open_row["execution_id"] != continues_execution_id:
                # Nothing live owns the task (checked above), so this execution is waiting
                # between attempts -- and this admission is not its next one.
                self._close(
                    connection,
                    open_row["execution_id"],
                    outcome="superseded",
                    reason=f"a new admission ({run_id}) for {project_id}/{task_id} replaced it",
                )
                open_row = None
            if continues_execution_id is not None and open_row is None:
                raise OwnershipConflict(
                    f"execution {continues_execution_id} is not open for {project_id}/{task_id}, "
                    f"so admission {run_id} cannot continue it"
                )
            if open_row is not None:
                execution_id = open_row["execution_id"]
            elif envelope is not None:
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
                    controlled_by=controlled_by,
                ).execution_id
            try:
                connection.execute(
                    "INSERT INTO run_attempt(run_id, execution_id, project_id, task_id, mode, "
                    "takes_slot, holder, holder_pid, state, reservation_json, admitted_at, "
                    "operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        attempt_operation_id,
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
                    "UPDATE execution SET state = 'launching', next_due_at = NULL "
                    "WHERE execution_id = ?",
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

    def release_slot(self, run_id: str, *, reason: str) -> Optional[Attempt]:
        """Give this live attempt's machine slot back, leaving the attempt live (task-482).

        Idempotent and one-way. A row that has already released keeps its first release's
        time and reason, and a terminal row is left alone -- its slot went back when it
        concluded, and stamping a release on it would put two endings on one run.

        Nothing else about the attempt changes: it still owns its task, it is still
        followed by the poller, it can still be stopped, and it still appears on every
        surface that lists what is running. The one thing it stops doing is standing
        between the next dispatch and a free machine.

        ``None`` when there is no row for ``run_id`` -- a pre-journal run, which is judged
        from its ``meta.yaml`` instead.
        """
        with self.transaction("release_slot") as connection:
            row = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            if row["state"] == ATTEMPT_TERMINAL or _column(row, "slot_released_at"):
                return Attempt.from_row(row)
            connection.execute(
                "UPDATE run_attempt SET slot_released_at = ?, slot_released_reason = ? "
                "WHERE run_id = ?",
                (_iso(self.now()), reason, run_id),
            )
            found = connection.execute(
                "SELECT * FROM run_attempt WHERE run_id = ?", (run_id,)
            ).fetchone()
            return Attempt.from_row(found)

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

    def request_stand_down(
        self,
        run_id: str,
        *,
        requester: str,
        source: str,
        reason: str,
        transfer_to: str,
        holder_pid: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Record that a live attempt is being asked to hand its task to someone else.

        **Not a cancellation, and the difference is the whole of task-312.** A Stop
        revokes the execution's intent: it sets ``cancel_requested``, bumps the control
        generation and refuses every later continuation. A stand-down does none of that.
        The work it ends was finished and approved, and the task is being transferred --
        to the scripted finish -- rather than abandoned, so nothing that reads a Stop may
        read one here.

        Recorded before anything is signalled, exactly as a Stop is, with the generation
        it was decided against. One per attempt: a second request returns the first.
        Raises ``ExecutionStoreError`` for an attempt with no execution to record it on --
        a pre-journal run -- because a transfer nobody can audit is not one to perform.
        """
        with self.transaction("stand-down") as connection:
            row = self._attempt_for_update(connection, run_id, epoch=None)
            if not row["execution_id"]:
                raise ExecutionStoreError(
                    f"attempt {run_id} has no execution to record a stand-down on"
                )
            existing = self._stand_down_row(connection, row["execution_id"], run_id)
            if existing is not None:
                return existing
            request = {
                "run_id": run_id,
                "requester": requester,
                "source": source,
                "reason": reason,
                "transfer_to": transfer_to,
                "holder_pid": holder_pid,
                "generation": int(row["control_generation"]),
                "requested_at": _iso(self.now()),
            }
            self._append(
                connection,
                row["execution_id"],
                "stand_down_requested",
                request,
                source_id=f"stand_down:{run_id}",
            )
            if row["state"] != ATTEMPT_TERMINAL:
                connection.execute(
                    "UPDATE execution SET state = 'standing_down' "
                    "WHERE execution_id = ? AND terminal = 0",
                    (row["execution_id"],),
                )
            return request

    def _stand_down_row(
        self, connection: sqlite3.Connection, execution_id: str, run_id: str
    ) -> Optional[Dict[str, Any]]:
        found = connection.execute(
            "SELECT payload_json FROM execution_event WHERE execution_id = ? AND source_id = ?",
            (execution_id, f"stand_down:{run_id}"),
        ).fetchone()
        return _loads(found["payload_json"], None) if found is not None else None

    def stand_down_request(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The stand-down recorded for this attempt, or ``None``."""
        rows = self._read(
            "SELECT e.payload_json FROM execution_event e JOIN run_attempt a "
            "ON a.execution_id = e.execution_id WHERE a.run_id = ? AND e.source_id = ?",
            (run_id, f"stand_down:{run_id}"),
        )
        return _loads(rows[0]["payload_json"], None) if rows else None

    def stop_requests(self, project_id: str, task_id: str) -> List[Dict[str, Any]]:
        """Every Stop ever requested against a run of this project/task, oldest first.

        What invalidates an approval (task-312): a person who stopped the run after
        approving it has made a newer decision than the approval, and the finish must not
        act on the older one. Terminal attempts are included on purpose -- a Stop that has
        already landed is still the newer decision.
        """
        rows = self._read(
            "SELECT run_id, cancel_json FROM run_attempt WHERE project_id = ? AND task_id = ? "
            "AND cancel_requested = 1",
            (project_id, task_id),
        )
        requests = [
            {"run_id": row["run_id"], **(_loads(row["cancel_json"], None) or {})} for row in rows
        ]
        return sorted(requests, key=lambda request: str(request.get("requested_at") or ""))

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
        retry_owed: bool = False,
        failure_class: Optional[str] = None,
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

        ``retry_owed`` (task-416) ends the attempt without ending its execution -- but only
        for an execution the durable controller drives, and never for a cancellation. The
        controller then decides from the recorded ``failure_class`` and the envelope's
        retry policy whether a next attempt is permitted. An execution the legacy poller
        follows closes with its attempt exactly as before, because nothing would ever come
        back for it.
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
                driven = connection.execute(
                    "SELECT controlled_by FROM execution WHERE execution_id = ?",
                    (row["execution_id"],),
                ).fetchone()
                keep_open = bool(
                    retry_owed
                    and outcome != "cancelled"
                    and not int(row["cancel_requested"])
                    and driven is not None
                    and driven["controlled_by"] == CONTROLLED_BY_CONTROLLER
                )
                payload: Dict[str, Any] = {
                    "run_id": run_id,
                    "outcome": outcome,
                    "status": status,
                    "by": concluded_by,
                }
                if keep_open:
                    payload.update(retry_owed=True, failure_class=failure_class)
                self._append(
                    connection,
                    row["execution_id"],
                    "concluded",
                    payload,
                    source_id=f"concluded:{run_id}",
                )
                if keep_open:
                    connection.execute(
                        "UPDATE execution SET state = 'retry_wait', updated_at = ? "
                        "WHERE execution_id = ?",
                        (now, row["execution_id"]),
                    )
                else:
                    final = "cancelled" if outcome == "cancelled" else "concluded"
                    connection.execute(
                        "UPDATE execution SET state = ?, terminal = 1, updated_at = ? "
                        "WHERE execution_id = ?",
                        (final, now, row["execution_id"]),
                    )
                    self._cancel_timers(connection, row["execution_id"])
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

    def attempt_by_operation(self, operation_id: str) -> Optional[Attempt]:
        """The attempt an admission with this stable operation id created, if any."""
        rows = self._read("SELECT * FROM run_attempt WHERE operation_id = ?", (operation_id,))
        return Attempt.from_row(rows[0]) if rows else None

    def attempts_for(self, execution_id: str) -> List[Attempt]:
        """Every attempt an execution has had, oldest first."""
        rows = self._read(
            "SELECT * FROM run_attempt WHERE execution_id = ? ORDER BY admitted_at, rowid",
            (execution_id,),
        )
        return [Attempt.from_row(row) for row in rows]

    # ----- closing an execution between attempts (task-416) ---------------------

    def _close(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
        *,
        outcome: str,
        reason: str,
        failure_class: Optional[str] = None,
    ) -> None:
        live = connection.execute(
            "SELECT run_id FROM run_attempt WHERE execution_id = ? AND state <> 'terminal'",
            (execution_id,),
        ).fetchone()
        if live is not None:
            raise OwnershipConflict(
                f"execution {execution_id} still has live attempt {live['run_id']}; an "
                "execution closes only between attempts"
            )
        self._append(
            connection,
            execution_id,
            "closed",
            {"outcome": outcome, "reason": reason, "failure_class": failure_class},
            source_id=f"closed:{execution_id}",
        )
        final = "cancelled" if outcome == "cancelled" else "concluded"
        connection.execute(
            "UPDATE execution SET state = ?, terminal = 1, next_due_at = NULL, updated_at = ? "
            "WHERE execution_id = ? AND terminal = 0",
            (final, _iso(self.now()), execution_id),
        )
        self._cancel_timers(connection, execution_id)

    def close_execution(
        self,
        execution_id: str,
        *,
        outcome: str,
        reason: str,
        failure_class: Optional[str] = None,
        epoch: Optional[int] = None,
    ) -> Execution:
        """End an execution that has no live attempt: exhausted, escalated or superseded.

        Idempotent -- a closed execution is returned as it is. ``epoch`` fences a
        controller that lost ownership of the execution in the meantime.
        """
        with self.transaction("close") as connection:
            row = connection.execute(
                "SELECT * FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"no execution {execution_id!r}")
            if not int(row["terminal"]):
                if epoch is not None and int(row["controller_epoch"]) != int(epoch):
                    raise StaleOwner(
                        f"execution {execution_id} is at controller epoch "
                        f"{row['controller_epoch']}, not {epoch}"
                    )
                self._close(
                    connection,
                    execution_id,
                    outcome=outcome,
                    reason=reason,
                    failure_class=failure_class,
                )
            found = connection.execute(
                "SELECT * FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return Execution.from_row(found)

    def stop_execution(
        self, execution_id: str, *, requester: str, source: str, reason: str = ""
    ) -> Optional[Execution]:
        """A Stop that lands between attempts: record who asked, then cancel the intent.

        With no live attempt there is no process to signal, so the Stop is complete once
        committed -- which is what keeps a restart from finding a retry to perform. With a
        live attempt this does nothing and returns ``None``: that Stop goes through
        ``request_cancel`` on the attempt, which confirms the process first.
        """
        with self.transaction("stop-execution") as connection:
            row = connection.execute(
                "SELECT * FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"no execution {execution_id!r}")
            if int(row["terminal"]):
                return Execution.from_row(row)
            live = connection.execute(
                "SELECT run_id FROM run_attempt WHERE execution_id = ? AND state <> 'terminal'",
                (execution_id,),
            ).fetchone()
            if live is not None:
                return None
            generation = int(row["control_generation"]) + 1
            self._append(
                connection,
                execution_id,
                "stop_requested",
                {
                    "generation": generation,
                    "requester": requester,
                    "source": source,
                    "reason": reason,
                    "requested_at": _iso(self.now()),
                },
                source_id=f"stop-execution:{execution_id}:{generation}",
            )
            connection.execute(
                "UPDATE execution SET control_generation = ? WHERE execution_id = ?",
                (generation, execution_id),
            )
            self._close(
                connection,
                execution_id,
                outcome="cancelled",
                reason=f"stopped by {requester} from {source}",
                failure_class="cancelled_by_user",
            )
            found = connection.execute(
                "SELECT * FROM execution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return Execution.from_row(found)

    # ----- timers (task-416) ------------------------------------------------------

    def _cancel_timers(self, connection: sqlite3.Connection, execution_id: str) -> None:
        connection.execute(
            "UPDATE timer SET cancelled_at = ? WHERE execution_id = ? AND fired_at IS NULL "
            "AND cancelled_at IS NULL",
            (_iso(self.now()), execution_id),
        )

    def _refresh_due(self, connection: sqlite3.Connection, execution_id: Optional[str]) -> None:
        if execution_id is None:
            return
        row = connection.execute(
            "SELECT MIN(due_at) AS due FROM timer WHERE execution_id = ? AND fired_at IS NULL "
            "AND cancelled_at IS NULL",
            (execution_id,),
        ).fetchone()
        connection.execute(
            "UPDATE execution SET next_due_at = ? WHERE execution_id = ?",
            (row["due"] if row is not None else None, execution_id),
        )

    def set_timer(
        self,
        timer_id: str,
        *,
        owner: str,
        kind: str,
        due_at: datetime,
        execution_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> "Timer":
        """Persist a due time under a stable id. A repeated set returns the existing timer.

        The deadline is a UTC moment on disk, never a sleeping thread: a process that dies
        with a timer outstanding leaves it for whichever process next asks what is due.
        An execution's timer also appends ``timer_set`` to its history in the same commit,
        so a replay knows what it is waiting for.
        """
        with self.transaction("timer-set") as connection:
            row = connection.execute(
                "SELECT * FROM timer WHERE timer_id = ?", (timer_id,)
            ).fetchone()
            if row is None:
                now = _iso(self.now())
                connection.execute(
                    "INSERT INTO timer(timer_id, owner, execution_id, kind, due_at, payload_json, "
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        timer_id,
                        owner,
                        execution_id,
                        kind,
                        _iso(due_at),
                        _dumps(dict(payload or {})),
                        now,
                    ),
                )
                if execution_id is not None:
                    self._append(
                        connection,
                        execution_id,
                        "timer_set",
                        {
                            "timer_id": timer_id,
                            "kind": kind,
                            "due_at": _iso(due_at),
                            **dict(payload or {}),
                        },
                        source_id=f"timer_set:{timer_id}",
                    )
                    self._refresh_due(connection, execution_id)
                row = connection.execute(
                    "SELECT * FROM timer WHERE timer_id = ?", (timer_id,)
                ).fetchone()
            return Timer.from_row(row)

    def timer(self, timer_id: str) -> Optional["Timer"]:
        rows = self._read("SELECT * FROM timer WHERE timer_id = ?", (timer_id,))
        return Timer.from_row(rows[0]) if rows else None

    def due_timers(
        self, *, now: Optional[datetime] = None, owner_prefix: str = ""
    ) -> List["Timer"]:
        """Timers whose due time has passed and which have neither fired nor been cancelled."""
        moment = _iso(now or self.now())
        rows = self._read(
            "SELECT * FROM timer WHERE fired_at IS NULL AND cancelled_at IS NULL AND due_at <= ? "
            "AND owner LIKE ? ORDER BY due_at, timer_id",
            (moment, owner_prefix + "%"),
        )
        return [Timer.from_row(row) for row in rows]

    def fire_timer(self, timer_id: str) -> bool:
        """Mark a due timer fired. True for exactly one caller, ever, per timer.

        The compare-and-set on ``fired_at`` is what makes a timer fire once however many
        processes find it due -- and it is what stops a machine waking from sleep with
        six overdue ticks from acting six times: each timer is one firing, and a periodic
        wait sets its *next* timer from the moment it fired, never from its old due time.
        """
        with self.transaction("timer-fire") as connection:
            row = connection.execute(
                "SELECT * FROM timer WHERE timer_id = ?", (timer_id,)
            ).fetchone()
            if row is None or row["fired_at"] is not None or row["cancelled_at"] is not None:
                return False
            now = _iso(self.now())
            cursor = connection.execute(
                "UPDATE timer SET fired_at = ? WHERE timer_id = ? AND fired_at IS NULL "
                "AND cancelled_at IS NULL",
                (now, timer_id),
            )
            if cursor.rowcount != 1:  # pragma: no cover - guarded by the read above
                return False
            if row["execution_id"] is not None:
                self._append(
                    connection,
                    row["execution_id"],
                    "timer_fired",
                    {"timer_id": timer_id, "kind": row["kind"], "fired_at": now},
                    source_id=f"timer_fired:{timer_id}",
                )
                self._refresh_due(connection, row["execution_id"])
            return True

    def cancel_timer(self, timer_id: str) -> bool:
        with self.transaction("timer-cancel") as connection:
            row = connection.execute(
                "SELECT execution_id FROM timer WHERE timer_id = ?", (timer_id,)
            ).fetchone()
            cursor = connection.execute(
                "UPDATE timer SET cancelled_at = ? WHERE timer_id = ? AND fired_at IS NULL "
                "AND cancelled_at IS NULL",
                (_iso(self.now()), timer_id),
            )
            if row is not None:
                self._refresh_due(connection, row["execution_id"])
            return cursor.rowcount == 1

    # ----- the machine's dispatch queue (task-459) --------------------------------

    def enqueue_dispatch(
        self,
        project_id: str,
        task_id: str,
        *,
        request: Mapping[str, Any],
        queued_by: str = "",
        source: str = "manual",
        limit: int = 0,
        queue_id: Optional[str] = None,
    ) -> QueuedDispatch:
        """Accept a dispatch that found no slot, as a durable place in line.

        The cap and the one-entry-per-task rule are enforced *inside* the transaction
        rather than by a read before it, for the reason every other admission here is:
        two processes both reading "there is room" is exactly the race a queue with a
        bound exists to lose safely.

        ``seq`` is allocated from the table's own maximum in the same transaction, so
        FIFO order is a stored fact rather than a sort over two clocks. Order by it and
        never by ``queued_at``: two entries queued in the same millisecond by different
        processes have one order here and no order there.
        """
        with self.transaction("queue-enqueue") as connection:
            existing = connection.execute(
                "SELECT * FROM dispatch_queue WHERE project_id = ? AND task_id = ? "
                "AND status IN ('queued', 'starting')",
                (project_id, task_id),
            ).fetchone()
            if existing is not None:
                raise AlreadyQueued(
                    f"{task_id} already has dispatch {existing['queue_id']} waiting in the "
                    f"queue ({existing['status']}); cancel it rather than queueing a second",
                    queue_id=existing["queue_id"],
                )
            depth = int(
                connection.execute(
                    "SELECT COUNT(*) FROM dispatch_queue WHERE status IN ('queued', 'starting')"
                ).fetchone()[0]
            )
            if limit > 0 and depth >= limit:
                raise QueueFull(
                    f"the dispatch queue holds {depth} waiting dispatch(es) and this machine's "
                    f"limits.dispatch_queue_limit is {limit}",
                    depth=depth,
                    limit=limit,
                )
            moment = _iso(self.now())
            seq = (
                int(
                    connection.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM dispatch_queue"
                    ).fetchone()[0]
                )
                + 1
            )
            identifier = queue_id or f"q_{uuid.uuid4().hex[:12]}"
            connection.execute(
                "INSERT INTO dispatch_queue(queue_id, seq, project_id, task_id, source, "
                "request_json, queued_by, status, queued_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'queued',?,?)",
                (
                    identifier,
                    seq,
                    project_id,
                    task_id,
                    source,
                    _dumps(dict(request)),
                    queued_by,
                    moment,
                    moment,
                ),
            )
            row = connection.execute(
                "SELECT * FROM dispatch_queue WHERE queue_id = ?", (identifier,)
            ).fetchone()
            return QueuedDispatch.from_row(row)

    def queued_dispatches(
        self, *, waiting_only: bool = True, limit: int = 0
    ) -> List[QueuedDispatch]:
        """Every entry still holding a place in line, oldest first. FIFO is ``seq``."""
        sql = "SELECT * FROM dispatch_queue"
        if waiting_only:
            sql += " WHERE status IN ('queued', 'starting')"
        sql += " ORDER BY seq"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        return [QueuedDispatch.from_row(row) for row in self._read(sql)]

    def queued_dispatch(self, queue_id: str) -> Optional[QueuedDispatch]:
        rows = self._read("SELECT * FROM dispatch_queue WHERE queue_id = ?", (queue_id,))
        return QueuedDispatch.from_row(rows[0]) if rows else None

    def queue_depth(self) -> int:
        return int(
            self._read(
                "SELECT COUNT(*) AS n FROM dispatch_queue WHERE status IN ('queued', 'starting')"
            )[0]["n"]
        )

    def claim_queued_dispatch(
        self,
        queue_id: str,
        *,
        stale_after_seconds: int = 900,
        retry_after_seconds: int = 0,
    ) -> Optional[QueuedDispatch]:
        """Take one entry out of the line to try starting it. ``None`` if somebody else has.

        The compare-and-set on ``status`` is what stops two ticks -- a server's and a
        hand-run ``agentjobs execution tick`` -- both spending a slot on the same entry.

        An entry left ``starting`` by a process that died is reclaimed after
        ``stale_after_seconds``. That is safe rather than hopeful: the retry passes the
        same ``admission_operation_id``, so if the dead process did admit a run, the
        second attempt is handed that attempt instead of starting a second one.

        ``retry_after_seconds`` holds an entry that was already tried and put back. The
        tick runs every few seconds and a condition that refused a start -- the task's own
        run still finishing, a hold -- clears on a human scale, so retrying at the tick's
        rate is work nobody asked for. ``claimed_at`` is kept across a release for this,
        which is why it means *last tried* rather than *currently held*.
        """
        with self.transaction("queue-claim") as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            if row is None or row["status"] not in QUEUE_OPEN_STATUSES:
                return None
            now = self.now()
            claimed = _parse_moment(row["claimed_at"])
            if row["status"] == QUEUE_STARTING:
                if claimed is None or (now - claimed).total_seconds() < stale_after_seconds:
                    return None
            elif (
                retry_after_seconds > 0
                and claimed is not None
                and (now - claimed).total_seconds() < retry_after_seconds
            ):
                return None
            moment = _iso(now)
            connection.execute(
                "UPDATE dispatch_queue SET status = 'starting', claimed_at = ?, updated_at = ?, "
                "attempts = attempts + 1 WHERE queue_id = ?",
                (moment, moment, queue_id),
            )
            return QueuedDispatch.from_row(
                connection.execute(
                    "SELECT * FROM dispatch_queue WHERE queue_id = ?", (queue_id,)
                ).fetchone()
            )

    def release_queued_dispatch(self, queue_id: str, *, detail: str = "") -> bool:
        """Put a claimed entry back in line, keeping its place. A transient refusal.

        ``claimed_at`` is deliberately left alone: it is the moment this entry was last
        tried, and it is what ``retry_after_seconds`` above measures.
        """
        with self.transaction("queue-release") as connection:
            cursor = connection.execute(
                "UPDATE dispatch_queue SET status = 'queued', detail = ?, "
                "updated_at = ? WHERE queue_id = ? AND status = 'starting'",
                (detail[:1000], _iso(self.now()), queue_id),
            )
            return cursor.rowcount == 1

    def settle_queued_dispatch(
        self,
        queue_id: str,
        *,
        status: str,
        run_id: Optional[str] = None,
        detail: str = "",
    ) -> bool:
        """Close an entry out of the line: started, refused, or cancelled."""
        if status not in (QUEUE_STARTED, QUEUE_REFUSED, QUEUE_CANCELLED):
            raise ValueError(f"{status!r} is not a terminal dispatch-queue status")
        with self.transaction("queue-settle") as connection:
            moment = _iso(self.now())
            cursor = connection.execute(
                "UPDATE dispatch_queue SET status = ?, run_id = ?, detail = ?, settled_at = ?, "
                "updated_at = ? WHERE queue_id = ? AND status IN ('queued', 'starting')",
                (status, run_id, detail[:1000], moment, moment, queue_id),
            )
            return cursor.rowcount == 1

    def purge_queue_history(self, *, keep: int = 200) -> int:
        """Drop the oldest settled entries beyond ``keep``. The waiting ones are untouched."""
        with self.transaction("queue-purge") as connection:
            cursor = connection.execute(
                "DELETE FROM dispatch_queue WHERE status NOT IN ('queued', 'starting') "
                "AND seq NOT IN (SELECT seq FROM dispatch_queue "
                "WHERE status NOT IN ('queued', 'starting') ORDER BY seq DESC LIMIT ?)",
                (int(keep),),
            )
            return cursor.rowcount

    # ----- the pull mode's arming record (task-462) --------------------------------

    def arm_pull(
        self,
        project_id: str,
        *,
        armed_by: str,
        bound_kind: str,
        bound_starts: Optional[int] = None,
        bound_until: Optional[str] = None,
        posture: Optional[str] = None,
        arming_id: Optional[str] = None,
    ) -> PullArming:
        """Record that a person switched the pull mode on for this project.

        One armed row per project, enforced by a partial unique index inside the
        transaction rather than by a read before it: two browsers arming the same project
        in the same second is the race a per-project mode has to lose safely, and the
        loser is told which arming won rather than silently creating a second budget.

        The bound is stored as it was chosen, not as a derived deadline. ``starts`` counts
        runs, which is the only bound that means the same thing on a machine whose clock
        moved; ``until`` is a wall-clock end; ``open`` is *until somebody disarms it*,
        which is a real answer and not the absence of one.
        """
        if bound_kind not in (BOUND_STARTS, BOUND_UNTIL, BOUND_OPEN):
            raise ValueError(f"{bound_kind!r} is not a pull-mode bound")
        if bound_kind == BOUND_STARTS and not (bound_starts and bound_starts > 0):
            raise ValueError("a `starts` bound needs a positive number of starts")
        if bound_kind == BOUND_UNTIL and not bound_until:
            raise ValueError("an `until` bound needs a moment to stop at")
        with self.transaction("pull-arm") as connection:
            existing = connection.execute(
                "SELECT * FROM pull_arming WHERE project_id = ? AND state = 'armed'",
                (project_id,),
            ).fetchone()
            if existing is not None:
                raise AlreadyQueued(
                    f"{project_id} is already armed for the pull mode by "
                    f"{existing['armed_by']!r} ({existing['arming_id']}); disarm it before "
                    "arming it again",
                    queue_id=existing["arming_id"],
                )
            moment = _iso(self.now())
            identifier = arming_id or f"arm_{uuid.uuid4().hex[:12]}"
            connection.execute(
                "INSERT INTO pull_arming(arming_id, project_id, armed_by, armed_at, "
                "bound_kind, bound_starts, bound_until, posture, state, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'armed',?)",
                (
                    identifier,
                    project_id,
                    armed_by,
                    moment,
                    bound_kind,
                    bound_starts if bound_kind == BOUND_STARTS else None,
                    bound_until if bound_kind == BOUND_UNTIL else None,
                    posture,
                    moment,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pull_arming WHERE arming_id = ?", (identifier,)
            ).fetchone()
            return PullArming.from_row(row)

    def armed_pulls(self) -> List[PullArming]:
        """Every project armed right now, oldest arming first. Round-robin order."""
        return [
            PullArming.from_row(row)
            for row in self._read(
                "SELECT * FROM pull_arming WHERE state = 'armed' ORDER BY armed_at, arming_id"
            )
        ]

    def armed_pull(self, project_id: str) -> Optional[PullArming]:
        """This project's live arming, or ``None`` when it is not armed."""
        rows = self._read(
            "SELECT * FROM pull_arming WHERE project_id = ? AND state = 'armed'", (project_id,)
        )
        return PullArming.from_row(rows[0]) if rows else None

    def pull_arming(self, arming_id: str) -> Optional[PullArming]:
        """One arming by its id, whatever state it is in. This is what a start reads."""
        rows = self._read("SELECT * FROM pull_arming WHERE arming_id = ?", (arming_id,))
        return PullArming.from_row(rows[0]) if rows else None

    def pull_armings(self, project_id: str, *, limit: int = 20) -> List[PullArming]:
        """This project's armings, newest first. The history behind the board's one line."""
        return [
            PullArming.from_row(row)
            for row in self._read(
                "SELECT * FROM pull_arming WHERE project_id = ? "
                f"ORDER BY armed_at DESC LIMIT {int(limit)}",
                (project_id,),
            )
        ]

    def spend_pull_start(self, arming_id: str) -> Optional[PullArming]:
        """Charge one start to this arming, refusing when the bound has no room left.

        The increment and the bound check are one statement, which is the whole reason
        this is a store method rather than arithmetic in the tick: two ticks -- a
        server's and a hand-run one -- both reading ``started < bound`` and both starting
        a run is exactly how a bound of three buys four.

        ``None`` means the arming is not armed any more, or its bound is spent. The
        caller retires it and says which; this method never changes ``state``, so a
        refusal here is always recoverable by reading the row.
        """
        with self.transaction("pull-spend") as connection:
            cursor = connection.execute(
                "UPDATE pull_arming SET started = started + 1, updated_at = ? "
                "WHERE arming_id = ? AND state = 'armed' "
                "AND (bound_kind <> 'starts' OR started < bound_starts)",
                (_iso(self.now()), arming_id),
            )
            if cursor.rowcount != 1:
                return None
            return PullArming.from_row(
                connection.execute(
                    "SELECT * FROM pull_arming WHERE arming_id = ?", (arming_id,)
                ).fetchone()
            )

    def refund_pull_start(self, arming_id: str) -> None:
        """Give back a start charged for a dispatch that then failed to launch.

        Charging before the dispatch is what makes the bound safe under two ticks; the
        refund is what stops a bound of three being spent by three refusals. It never
        goes below zero and never touches ``failures``, which
        :meth:`record_pull_failure` owns.
        """
        with self.transaction("pull-refund") as connection:
            connection.execute(
                "UPDATE pull_arming SET started = MAX(0, started - 1), updated_at = ? "
                "WHERE arming_id = ?",
                (_iso(self.now()), arming_id),
            )

    def clear_pull_failures(self, arming_id: str) -> None:
        """Forget this arming's run of failed starts, because one has just succeeded.

        Separate from :meth:`spend_pull_start` rather than folded into it, because the
        bound is charged *before* the dispatch and a failure is only known afterwards.
        Resetting at the charge would make the run of three unreachable: every failed
        start would clear the count its own failure was about to add to.
        """
        with self.transaction("pull-recovered") as connection:
            connection.execute(
                "UPDATE pull_arming SET failures = 0, updated_at = ? WHERE arming_id = ?",
                (_iso(self.now()), arming_id),
            )

    def record_pull_failure(self, arming_id: str) -> int:
        """Count one start that failed, and answer how many have failed in a row.

        Reset to zero by :meth:`clear_pull_failures`, so this counts *consecutive* failures
        rather than failures. A machine-wide fault -- a runner that no longer launches, a
        login that has expired -- fails every task it is offered, and a mode that kept
        offering would burn the hourly cap on nothing; a run of three is the tick's
        signal to stop and say so.
        """
        with self.transaction("pull-failure") as connection:
            connection.execute(
                "UPDATE pull_arming SET failures = failures + 1, updated_at = ? "
                "WHERE arming_id = ?",
                (_iso(self.now()), arming_id),
            )
            row = connection.execute(
                "SELECT failures FROM pull_arming WHERE arming_id = ?", (arming_id,)
            ).fetchone()
            return int(row["failures"]) if row is not None else 0

    def retire_pull(
        self,
        arming_id: str,
        *,
        state: str,
        retired_by: str = "",
        detail: str = "",
    ) -> Optional[PullArming]:
        """End an arming. ``None`` when it had already ended, so a double disarm is quiet.

        The compare-and-set on ``state`` is what makes a disarm idempotent: a person
        pressing Disarm twice, or pressing it in the same second a bound runs out, gets
        one retirement and one reason rather than the second overwriting the first.
        """
        if state not in PULL_RETIRED_STATES:
            raise ValueError(f"{state!r} is not a way an arming ends")
        with self.transaction("pull-retire") as connection:
            moment = _iso(self.now())
            cursor = connection.execute(
                "UPDATE pull_arming SET state = ?, retired_at = ?, retired_by = ?, "
                "detail = ?, updated_at = ? WHERE arming_id = ? AND state = 'armed'",
                (state, moment, retired_by, detail[:1000], moment, arming_id),
            )
            if cursor.rowcount != 1:
                return None
            return PullArming.from_row(
                connection.execute(
                    "SELECT * FROM pull_arming WHERE arming_id = ?", (arming_id,)
                ).fetchone()
            )

    def mark_controlled(self, execution_id: str, *, controlled_by: Optional[str]) -> None:
        """Hand an execution to the durable controller, or back. Quiescent boundary only."""
        with self.transaction("controlled-by") as connection:
            live = connection.execute(
                "SELECT run_id FROM run_attempt WHERE execution_id = ? AND state <> 'terminal'",
                (execution_id,),
            ).fetchone()
            if live is not None:
                raise OwnerModeConflict(
                    f"execution {execution_id} still has live attempt {live['run_id']}; drain "
                    "it before changing which controller drives the execution"
                )
            connection.execute(
                "UPDATE execution SET controlled_by = ?, controller_epoch = controller_epoch + 1 "
                "WHERE execution_id = ?",
                (controlled_by, execution_id),
            )
            self._append(
                connection,
                execution_id,
                "migrated",
                {"controlled_by": controlled_by, "reason": "controller routing changed"},
                source_id=f"controlled:{controlled_by}:{os.urandom(4).hex()}",
            )

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
            if not int(row["shadow"]):
                # The result is history the reducer decides from (task-416), so it is an
                # event of the execution, committed with the activity row it describes.
                # Shadow proposals record nothing a replay would read.
                self._append(
                    connection,
                    row["execution_id"],
                    "activity_result",
                    {
                        "activity_id": activity_id,
                        "kind": row["kind"],
                        "state": state,
                        "error_class": error_class,
                        "result": dict(result) if result is not None else None,
                    },
                    source_id=f"result:{activity_id}:{int(row['attempt_count']) + 1}",
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

    def signal(self, source: str, source_event_id: str) -> Optional[InboxItem]:
        rows = self._read(
            "SELECT * FROM inbox WHERE source = ? AND source_event_id = ?",
            (source, source_event_id),
        )
        return InboxItem.from_row(rows[0]) if rows else None

    def signal_disposition(self, source: str, source_event_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read(
            "SELECT disposition_json FROM inbox WHERE source = ? AND source_event_id = ?",
            (source, source_event_id),
        )
        return _loads(rows[0]["disposition_json"], None) if rows else None

    def accept_signal(self, source: str, event: SourceEvent) -> InboxItem:
        """Put one source event in the inbox now, **without moving the source's cursor**.

        The synchronous half of an accepted approval (task-312): the route that wrote the
        handoff records it here before answering, so a server that dies in the next second
        leaves the approval in the journal rather than only in a task log nobody has
        imported yet. The row is exactly the one the feed import would write -- same
        source, same id, same payload -- so whichever of the two lands first is kept and
        the other is a no-op.

        The cursor is left alone because this caller does not know the feed position of
        everything *before* this entry, and advancing past it would lose those events.
        """
        payload = {
            "entry_id": event.entry_id,
            "type": event.type,
            "actor": event.actor,
            "ts": event.ts,
            "data": event.data,
        }
        status = "pending" if event.type in SIGNAL_ENTRY_TYPES else "observed"
        with self.transaction("accept-signal") as connection:
            open_row = connection.execute(
                "SELECT execution_id, control_generation FROM execution "
                "WHERE project_id = ? AND task_id = ? AND terminal = 0",
                (event.project_id, event.task_id),
            ).fetchone()
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
                    _iso(self.now()),
                ),
            )
            if cursor.rowcount == 1 and open_row is not None and status == "pending":
                self._append(
                    connection,
                    open_row["execution_id"],
                    "signal",
                    {"source_event_id": event.source_event_id, "kind": event.type},
                    source_id=f"signal:{source}:{event.source_event_id}",
                )
            found = connection.execute(
                "SELECT * FROM inbox WHERE source = ? AND source_event_id = ?",
                (source, event.source_event_id),
            ).fetchone()
            return InboxItem.from_row(found)

    def dispose_signal(
        self,
        source: str,
        source_event_id: str,
        *,
        status: str,
        disposition: Mapping[str, Any],
    ) -> Optional[InboxItem]:
        """Give one pending signal its explicit disposition. Nothing is ever deleted.

        ``consumed`` means it reached the thing it was for; ``superseded`` means a newer
        decision replaced it, and ``disposition`` names that decision; ``stale`` means its
        target no longer exists. Only a ``pending`` row moves -- a disposition is a fact
        about what happened to a message, and a second, contradictory one would be the
        latest-wins overwrite §9a rules out. Returns the row as it stands, or ``None`` when
        the signal was never imported.
        """
        if status not in {"consumed", "superseded", "stale"}:
            raise ValueError(f"not a signal disposition: {status!r}")
        with self.transaction("dispose-signal") as connection:
            row = connection.execute(
                "SELECT * FROM inbox WHERE source = ? AND source_event_id = ?",
                (source, source_event_id),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                recorded = {**dict(disposition), "at": _iso(self.now())}
                connection.execute(
                    "UPDATE inbox SET status = ?, disposition_json = ?, acknowledged_at = ? "
                    "WHERE inbox_id = ?",
                    (status, _dumps(recorded), recorded["at"], row["inbox_id"]),
                )
                if row["execution_id"]:
                    self._append(
                        connection,
                        row["execution_id"],
                        "signal_disposed",
                        {"source_event_id": source_event_id, "status": status, **recorded},
                        source_id=f"disposed:{source}:{source_event_id}",
                    )
            found = connection.execute(
                "SELECT * FROM inbox WHERE inbox_id = ?", (row["inbox_id"],)
            ).fetchone()
            return InboxItem.from_row(found)

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

    # ----- durable supervision (task-416 part 2, from task-418) --------------------

    def open_walk(
        self,
        *,
        project_id: str,
        parent_task_id: str,
        authority_entry: int,
        authority_actor: str,
        settings: Mapping[str, Any],
        host: str = "process",
        holder: Optional[str] = None,
        holder_pid: Optional[int] = None,
        holder_alive: Callable[[int], bool] = lambda _pid: True,
    ) -> Tuple[Supervision, bool]:
        """Start supervising an epic on one authorisation, or resume the walk that already is.

        Returns ``(walk, resumed)``. A walk is keyed by the parent and the human entry that
        authorised it, so a supervisor that died and is started again on the same
        authorisation resumes *that* walk -- its admitted children, reserved attempts and
        grounding -- rather than beginning a new one that has forgotten them. A walk whose
        holder process is still alive is never taken over: two supervisors of one epic is
        the duplicate-launch the record exists to prevent.

        A fresh authorisation of the epic supersedes the old walk, which is the existing
        escape hatch for a child that burned its attempts: the new walk's budget is new.
        """
        who, pid = this_holder()
        who = holder or who
        pid = holder_pid if holder_pid is not None else pid
        with self.transaction("walk-open") as connection:
            row = connection.execute(
                "SELECT * FROM supervision WHERE project_id = ? AND parent_task_id = ? "
                "AND state = 'walking'",
                (project_id, parent_task_id),
            ).fetchone()
            now = _iso(self.now())
            if row is not None:
                current = Supervision.from_row(row)
                other = current.holder is not None and current.holder != who
                if (
                    other
                    and current.holder_pid is not None
                    and current.host == "process"
                    and holder_alive(int(current.holder_pid))
                    and not process_created_after(
                        int(current.holder_pid), datetime.fromisoformat(row["updated_at"])
                    )
                ):
                    # Named by pid and time, because the reader of this is deciding which
                    # of two processes to stop (task-444): a walker they believed was gone
                    # is the usual cause, and a pid is what finds it.
                    raise OwnershipConflict(
                        f"{project_id}/{parent_task_id} is already being walked by pid "
                        f"{current.holder_pid} ({current.holder}), walk {current.walk_id} "
                        f"opened {row['created_at']} and last recorded {row['updated_at']}; "
                        "one supervisor per epic, so this walk started nothing"
                    )
                if current.authority_entry == int(authority_entry):
                    connection.execute(
                        "UPDATE supervision SET holder = ?, holder_pid = ?, host = ?, "
                        "epoch = epoch + 1, updated_at = ? WHERE walk_id = ?",
                        (who, pid, host, now, current.walk_id),
                    )
                    found = connection.execute(
                        "SELECT * FROM supervision WHERE walk_id = ?", (current.walk_id,)
                    ).fetchone()
                    return Supervision.from_row(found), True
                connection.execute(
                    "UPDATE supervision SET state = 'stopped', stop = 'superseded', detail = ?, "
                    "updated_at = ? WHERE walk_id = ?",
                    (
                        f"superseded by a fresh authorisation (entry {authority_entry})",
                        now,
                        current.walk_id,
                    ),
                )
            walk_id = f"walk_{os.urandom(6).hex()}"
            connection.execute(
                "INSERT INTO supervision(walk_id, project_id, parent_task_id, authority_entry, "
                "authority_actor, settings_json, state, host, holder, holder_pid, epoch, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,'walking',?,?,?,1,?,?)",
                (
                    walk_id,
                    project_id,
                    parent_task_id,
                    int(authority_entry),
                    authority_actor,
                    _dumps(dict(settings)),
                    host,
                    who,
                    pid,
                    now,
                    now,
                ),
            )
            found = connection.execute(
                "SELECT * FROM supervision WHERE walk_id = ?", (walk_id,)
            ).fetchone()
            return Supervision.from_row(found), False

    def walk(self, walk_id: str) -> Optional[Supervision]:
        rows = self._read("SELECT * FROM supervision WHERE walk_id = ?", (walk_id,))
        return Supervision.from_row(rows[0]) if rows else None

    def open_walks(self, *, project_id: Optional[str] = None) -> List[Supervision]:
        if project_id is None:
            rows = self._read(
                "SELECT * FROM supervision WHERE state = 'walking' ORDER BY created_at"
            )
        else:
            rows = self._read(
                "SELECT * FROM supervision WHERE state = 'walking' AND project_id = ? "
                "ORDER BY created_at",
                (project_id,),
            )
        return [Supervision.from_row(row) for row in rows]

    def _walk_for_update(
        self, connection: sqlite3.Connection, walk_id: str, epoch: int
    ) -> sqlite3.Row:
        row: Optional[sqlite3.Row] = connection.execute(
            "SELECT * FROM supervision WHERE walk_id = ?", (walk_id,)
        ).fetchone()
        if row is None:
            raise ExecutionStoreError(f"no walk {walk_id!r}")
        if int(row["epoch"]) != int(epoch):
            raise StaleOwner(
                f"walk {walk_id} is at epoch {row['epoch']}, not {epoch}; another supervisor "
                "took it over and this write was refused"
            )
        return row

    def update_walk(
        self,
        walk_id: str,
        *,
        epoch: int,
        grounding: Optional[Mapping[str, Any]] = None,
        state: Optional[str] = None,
        stop: Optional[str] = None,
        detail: Optional[str] = None,
        started: Optional[int] = None,
        peak_in_flight: Optional[int] = None,
        host: Optional[str] = None,
        clear_grounding: bool = False,
    ) -> Supervision:
        """Record what a walk decided. Grounding is sticky: the first cause is kept.

        ``clear_grounding`` is the one exception, and it exists for exactly one caller
        (task-467): a walk grounded because a child was parked on a person is grounded
        on a condition that *clears*, and when the person resolves that child the walk
        has to be able to take off again. Stickiness is right for every other cause --
        a child that died stays the reason the epic stopped -- so this is a deliberate
        lift rather than a relaxation of the rule, and it is refused unless the caller
        asks for it by name. Passing both it and ``grounding`` sets the new one.
        """
        with self.transaction("walk-update") as connection:
            self._walk_for_update(connection, walk_id, epoch)
            if clear_grounding and grounding is None:
                connection.execute(
                    "UPDATE supervision SET grounding_json = NULL WHERE walk_id = ?", (walk_id,)
                )
            elif clear_grounding:
                connection.execute(
                    "UPDATE supervision SET grounding_json = ? WHERE walk_id = ?",
                    (_dumps(dict(grounding or {})), walk_id),
                )
            connection.execute(
                "UPDATE supervision SET "
                "grounding_json = COALESCE(grounding_json, ?), "
                "state = COALESCE(?, state), stop = COALESCE(?, stop), "
                "detail = COALESCE(?, detail), "
                "started = MAX(started, COALESCE(?, 0)), "
                "peak_in_flight = MAX(peak_in_flight, COALESCE(?, 0)), "
                "host = COALESCE(?, host), updated_at = ? WHERE walk_id = ?",
                (
                    _dumps(dict(grounding)) if grounding is not None else None,
                    state,
                    stop,
                    detail,
                    started,
                    peak_in_flight,
                    host,
                    _iso(self.now()),
                    walk_id,
                ),
            )
            found = connection.execute(
                "SELECT * FROM supervision WHERE walk_id = ?", (walk_id,)
            ).fetchone()
            return Supervision.from_row(found)

    def supervised_children(self, walk_id: str) -> List[SupervisedChild]:
        rows = self._read(
            "SELECT * FROM supervision_child WHERE walk_id = ? ORDER BY seq", (walk_id,)
        )
        return [SupervisedChild.from_row(row) for row in rows]

    def supervised_child(self, walk_id: str, child_task_id: str) -> Optional[SupervisedChild]:
        rows = self._read(
            "SELECT * FROM supervision_child WHERE walk_id = ? AND child_task_id = ?",
            (walk_id, child_task_id),
        )
        return SupervisedChild.from_row(rows[0]) if rows else None

    def reserve_child_attempt(
        self,
        walk_id: str,
        *,
        epoch: int,
        child_task_id: str,
        operation_id: str,
        limit: int,
        used_on_record: int,
    ) -> Optional[SupervisedChild]:
        """Reserve one of a child's attempts before its dispatch, with the admission id.

        ``None`` when the attempts are spent. The number used is the larger of what this
        walk reserved and what the child's own log records, so neither a supervisor that
        died after reserving nor one that died after the child's authorising entry landed
        can be tricked into a third run. Refused outright on a grounded walk: grounding
        stops every further takeoff, including one a resumed supervisor was about to make.
        """
        with self.transaction("walk-reserve") as connection:
            walk = self._walk_for_update(connection, walk_id, epoch)
            if walk["grounding_json"] is not None or walk["state"] != "walking":
                raise OwnershipConflict(
                    f"walk {walk_id} is grounded or finished; no child may take off"
                )
            row = connection.execute(
                "SELECT * FROM supervision_child WHERE walk_id = ? AND child_task_id = ?",
                (walk_id, child_task_id),
            ).fetchone()
            now = _iso(self.now())
            if row is None:
                seq = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM supervision_child WHERE walk_id = ?",
                    (walk_id,),
                ).fetchone()[0]
                if int(used_on_record) >= int(limit):
                    return None
                connection.execute(
                    "INSERT INTO supervision_child(walk_id, child_task_id, seq, status, "
                    "attempts_reserved, operation_id, updated_at) VALUES (?,?,?,'admitting',?,?,?)",
                    (walk_id, child_task_id, int(seq), int(used_on_record) + 1, operation_id, now),
                )
            else:
                if row["status"] in ("admitting", "flying"):
                    raise OwnershipConflict(
                        f"{child_task_id} is already {row['status']} under walk {walk_id} "
                        f"({row['operation_id']}); reconcile it before another takeoff"
                    )
                reserved = int(row["attempts_reserved"])
                used = max(reserved, int(used_on_record))
                if used >= int(limit):
                    return None
                connection.execute(
                    "UPDATE supervision_child SET status = 'admitting', attempts_reserved = ?, "
                    "operation_id = ?, run_id = NULL, execution_id = NULL, deadline_at = NULL, "
                    "updated_at = ? WHERE walk_id = ? AND child_task_id = ?",
                    (used + 1, operation_id, now, walk_id, child_task_id),
                )
            found = connection.execute(
                "SELECT * FROM supervision_child WHERE walk_id = ? AND child_task_id = ?",
                (walk_id, child_task_id),
            ).fetchone()
            return SupervisedChild.from_row(found)

    def record_child(
        self,
        walk_id: str,
        *,
        epoch: int,
        child_task_id: str,
        status: str,
        run_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        deadline_at: Optional[datetime] = None,
        landed: Optional[Mapping[str, Any]] = None,
        refund: bool = False,
    ) -> SupervisedChild:
        """Move one child: admitted into flight, landed, owed a retry, or refunded.

        ``refund`` returns the reservation an admission provably never used -- the
        admitting operation id has no attempt in the journal and the process that held it
        is gone -- so a crash before admission costs the child nothing.
        """
        if status not in ("admitting", "flying", "landed", "retry_owed", "grounded"):
            raise ValueError(f"unknown child status {status!r}")
        with self.transaction("walk-child") as connection:
            self._walk_for_update(connection, walk_id, epoch)
            row = connection.execute(
                "SELECT * FROM supervision_child WHERE walk_id = ? AND child_task_id = ?",
                (walk_id, child_task_id),
            ).fetchone()
            if row is None:
                raise ExecutionStoreError(f"walk {walk_id} has no child {child_task_id}")
            history = list(_loads(row["history_json"], []))
            if landed is not None:
                history.append(dict(landed))
            connection.execute(
                "UPDATE supervision_child SET status = ?, "
                "run_id = COALESCE(?, run_id), execution_id = COALESCE(?, execution_id), "
                "deadline_at = COALESCE(?, deadline_at), history_json = ?, "
                "attempts_reserved = attempts_reserved - ?, updated_at = ? "
                "WHERE walk_id = ? AND child_task_id = ?",
                (
                    status,
                    run_id,
                    execution_id,
                    _iso(deadline_at) if deadline_at is not None else None,
                    _dumps(history),
                    1 if refund and int(row["attempts_reserved"]) > 0 else 0,
                    _iso(self.now()),
                    walk_id,
                    child_task_id,
                ),
            )
            found = connection.execute(
                "SELECT * FROM supervision_child WHERE walk_id = ? AND child_task_id = ?",
                (walk_id, child_task_id),
            ).fetchone()
            return SupervisedChild.from_row(found)

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
    "CONTROLLED_BY_CONTROLLER",
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
    "QUEUE_CANCELLED",
    "QUEUE_OPEN_STATUSES",
    "QUEUE_REFUSED",
    "QUEUE_STARTED",
    "QUEUE_STARTING",
    "QUEUE_WAITING",
    "BOUND_OPEN",
    "BOUND_STARTS",
    "BOUND_UNTIL",
    "PULL_ARMED",
    "PULL_DISARMED",
    "PULL_EXPIRED",
    "PULL_FAULTED",
    "PULL_RETIRED_STATES",
    "PULL_SPENT",
    "PullArming",
    "QueuedDispatch",
    "SCHEMA_REVISION",
    "SCHEMA_VERSION",
    "SourceEvent",
    "SupervisedChild",
    "Supervision",
    "Timer",
    "StoredEvent",
    "classify",
    "digest",
    "restore_snapshot",
    "this_holder",
]
