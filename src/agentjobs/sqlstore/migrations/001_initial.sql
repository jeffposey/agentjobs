-- AgentJobs authoritative SQLite store, physical schema version 1.
--
-- Designed on task-273 and coordinated with docs/analytics-design.md section 6.
-- Read docs/storage-sqlite.md for *why* any of this is shaped the way it is; this
-- file is the enforcement.
--
-- The *document* schema stays `schema: 2` (models_v2). This file's version is a
-- separate axis, held in `PRAGMA user_version`, because a physical migration must
-- not invalidate an exported task document.

-- ---------------------------------------------------------------------------
-- Bookkeeping
-- ---------------------------------------------------------------------------

CREATE TABLE schema_migration (
  version           INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  applied_at        TEXT NOT NULL,
  agentjobs_version TEXT NOT NULL,
  duration_ms       INTEGER
);

CREATE TABLE project (
  project_id            TEXT PRIMARY KEY,
  root                  TEXT,
  created_at            TEXT NOT NULL,
  -- An IANA zone name such as 'America/Chicago', never a fixed offset
  -- (analytics-design section 3.5, item C): '-06:00' is right for half the year.
  -- SQLite cannot resolve zone names, so day bucketing happens in Python.
  reporting_tz          TEXT NOT NULL DEFAULT 'UTC',
  imported_from         TEXT,
  imported_at           TEXT,
  history_baseline_at   TEXT,
  history_baseline_kind TEXT NOT NULL DEFAULT 'native'
      CHECK (history_baseline_kind IN ('native', 'reconstructed', 'backfilled', 'unknown'))
);

-- ---------------------------------------------------------------------------
-- Current state. One row per task; every filtered or grouped field is a column.
-- ---------------------------------------------------------------------------

CREATE TABLE task (
  project_id        TEXT NOT NULL REFERENCES project(project_id) ON DELETE RESTRICT,
  task_id           TEXT NOT NULL,
  seq               INTEGER,
  title             TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  revision          INTEGER NOT NULL DEFAULT 1,

  lifecycle         TEXT NOT NULL CHECK (lifecycle IN ('draft', 'ready', 'active', 'closed')),
  ball              TEXT CHECK (ball IN ('agent', 'human', 'external')),
  ball_reason       TEXT,
  ball_prompt       TEXT,
  outcome           TEXT CHECK (outcome IN ('completed', 'cancelled', 'superseded', 'duplicate')),
  archived          INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),

  priority          TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
  priority_rank     INTEGER NOT NULL GENERATED ALWAYS AS (
                      CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                    WHEN 'medium'   THEN 2 ELSE 3 END) STORED,
  queue_position    INTEGER CHECK (queue_position IS NULL OR queue_position >= 1),
  category          TEXT NOT NULL,
  effort            TEXT,
  owner             TEXT,
  parent_id         TEXT,
  posture           TEXT CHECK (posture IS NULL OR posture IN
                      ('read_only', 'auto', 'supervised', 'autonomous')),

  spec_summary      TEXT NOT NULL,
  spec_intent       TEXT,
  spec_description  TEXT NOT NULL,
  spec_constraints  TEXT,
  spec_out_of_scope TEXT,
  spec_context_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(spec_context_json)),
  links_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(links_json)),
  eligible_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(eligible_json)),

  -- Denormalised from task_event/log_entry, written in the same transaction as the
  -- thing that changes them, so they cannot drift the way a rebuilt cache would.
  closed_at         TEXT,
  first_claimed_at  TEXT,
  last_activity_at  TEXT NOT NULL,
  log_count         INTEGER NOT NULL DEFAULT 0,

  PRIMARY KEY (project_id, task_id),

  -- DEFERRABLE because an importer receives a task graph in arbitrary order and
  -- must not have to topologically sort it (analytics-design section 6, item G).
  FOREIGN KEY (project_id, parent_id) REFERENCES task(project_id, task_id)
      ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,

  -- models_v2._check_consistency rules 1-6, as constraints rather than a validator.
  CHECK (parent_id IS NULL OR parent_id <> task_id),
  CHECK ((lifecycle = 'closed') = (ball IS NULL)),                        -- rule 1
  CHECK ((lifecycle = 'closed') = (outcome IS NOT NULL)),                 -- rule 3
  CHECK ((lifecycle = 'closed') = (queue_position IS NULL)),              -- rule 6
  CHECK ((ball IS NULL) = (ball_reason IS NULL)),                         -- rule 2a
  CHECK (ball IS NULL                                                     -- rule 2b
      OR (ball = 'agent'    AND ball_reason IN
            ('available', 'work', 'revise', 'answer', 'redirect', 'hold'))
      OR (ball = 'human'    AND ball_reason IN
            ('spec', 'review', 'decision', 'approval', 'input'))
      OR (ball = 'external' AND ball_reason IN ('dependency', 'service'))),
  CHECK (ball IS NULL OR ball_reason = 'available'                        -- rule 4
      OR (ball_prompt IS NOT NULL AND trim(ball_prompt) <> '')),
  CHECK (lifecycle NOT IN ('draft', 'ready') OR owner IS NULL),           -- rule 5a
  CHECK (lifecycle <> 'active' OR owner IS NOT NULL),                     -- rule 5b
  CHECK ((lifecycle = 'closed') = (closed_at IS NOT NULL))
);

-- The invariant validation._check_queue enforces by scanning the corpus. Audit
-- finding F4 (the position race) is unrepresentable under this index.
CREATE UNIQUE INDEX ux_task_queue_slot
  ON task(project_id, priority_rank, queue_position)
  WHERE queue_position IS NOT NULL;

CREATE INDEX ix_task_queue_order ON task(project_id, priority_rank, queue_position)
  WHERE queue_position IS NOT NULL;
CREATE INDEX ix_task_ball      ON task(project_id, ball, ball_reason) WHERE lifecycle <> 'closed';
CREATE INDEX ix_task_open_age  ON task(project_id, created_at)        WHERE lifecycle <> 'closed';
CREATE INDEX ix_task_parent    ON task(project_id, parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX ix_task_owner     ON task(project_id, owner)     WHERE owner IS NOT NULL;
CREATE INDEX ix_task_lifecycle ON task(project_id, lifecycle, archived);
CREATE INDEX ix_task_closed_at ON task(project_id, closed_at) WHERE closed_at IS NOT NULL;
CREATE INDEX ix_task_activity  ON task(project_id, last_activity_at DESC);
-- analytics-design section 6, item E: the page's five counts, 14.6 ms -> 1.68 ms at 20x.
CREATE INDEX ix_task_counts    ON task(project_id, lifecycle, ball, outcome);

-- ---------------------------------------------------------------------------
-- Relationships and the value-object lists that are filtered or element-mutated
-- ---------------------------------------------------------------------------

CREATE TABLE task_tag (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  tag        TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  PRIMARY KEY (project_id, task_id, tag),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
CREATE INDEX ix_tag_lookup ON task_tag(project_id, tag);

CREATE TABLE task_dependency (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  other_id   TEXT NOT NULL,
  type       TEXT NOT NULL CHECK (type IN ('needs', 'blocks', 'related')),
  note       TEXT,
  ord        INTEGER NOT NULL,
  PRIMARY KEY (project_id, task_id, other_id, type),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE,
  CHECK (other_id <> task_id)
);
-- "who is waiting on me"
CREATE INDEX ix_dep_reverse ON task_dependency(project_id, other_id, type);

CREATE TABLE task_acceptance (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  ac_id      TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  text       TEXT NOT NULL,
  verify     TEXT,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'met', 'failed', 'dropped')),
  PRIMARY KEY (project_id, task_id, ac_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
CREATE INDEX ix_acceptance_status ON task_acceptance(project_id, status);

CREATE TABLE task_deliverable (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  path       TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  note       TEXT,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'done', 'dropped')),
  PRIMARY KEY (project_id, task_id, path),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);

CREATE TABLE task_branch (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  name       TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('active', 'merged', 'abandoned')),
  merged_at  TEXT,
  ord        INTEGER NOT NULL,
  PRIMARY KEY (project_id, task_id, name),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
-- what `agentjobs branches` reads today by parsing every task file
CREATE INDEX ix_branch_status ON task_branch(project_id, status);

-- ---------------------------------------------------------------------------
-- The append-only log
-- ---------------------------------------------------------------------------

CREATE TABLE log_entry (
  project_id  TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  entry_id    INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN (
                'note', 'progress', 'transition', 'handoff', 'decision', 'question',
                'answer', 'instruction', 'dispatch', 'dispatch_result', 'queue_move')),
  body        TEXT,
  re          INTEGER,
  data_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),
  PRIMARY KEY (project_id, task_id, entry_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE,
  FOREIGN KEY (project_id, task_id, re) REFERENCES log_entry(project_id, task_id, entry_id)
      DEFERRABLE INITIALLY DEFERRED,
  CHECK (re IS NULL OR re < entry_id)
);
CREATE INDEX ix_log_project_ts ON log_entry(project_id, ts DESC);
CREATE INDEX ix_log_type_ts    ON log_entry(project_id, type, ts DESC);
CREATE INDEX ix_log_thread     ON log_entry(project_id, task_id, re) WHERE re IS NOT NULL;
CREATE INDEX ix_log_questions  ON log_entry(project_id, task_id, entry_id) WHERE type = 'question';

-- Content-addressed, so the same image referenced twice is stored once and an
-- orphan is a refcount of zero rather than a directory scan. Blobs live in the
-- database (owner decision, task-273 fork 2), which is what makes one snapshot
-- the whole backup.
CREATE TABLE blob (
  sha256     TEXT PRIMARY KEY,
  media_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes >= 1),
  content    BLOB NOT NULL
);

CREATE TABLE attachment (
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  entry_id   INTEGER NOT NULL,
  ord        INTEGER NOT NULL,
  sha256     TEXT NOT NULL REFERENCES blob(sha256) ON DELETE RESTRICT,
  label      TEXT NOT NULL,
  PRIMARY KEY (project_id, task_id, entry_id, ord),
  FOREIGN KEY (project_id, task_id, entry_id)
      REFERENCES log_entry(project_id, task_id, entry_id) ON DELETE CASCADE
);
CREATE INDEX ix_attachment_blob ON attachment(sha256);

-- ---------------------------------------------------------------------------
-- Runs. Authoritative for what was dispatched: a run id carries exactly one
-- `dispatch` entry, and that entry's payload is rendered from these rows.
--
-- NOT authoritative for how a run ended. Two run ids in this repository's corpus
-- carry two `dispatch_result` entries each (interrupted, then completed) and a
-- single row cannot hold both. The terminal payload lives on the log entry that
-- recorded it; the columns below are a latest-observed projection kept so that
-- `ix_run_live` can answer "which runs are in the air" without a join.
-- ---------------------------------------------------------------------------

CREATE TABLE task_run (
  project_id        TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  task_id           TEXT NOT NULL,
  agent             TEXT NOT NULL,
  runner            TEXT NOT NULL,
  mode              TEXT NOT NULL CHECK (mode IN ('session', 'batch', 'interactive')),
  posture           TEXT NOT NULL,
  posture_source    TEXT,
  posture_ceiling   TEXT,
  posture_requested TEXT,
  trigger           TEXT NOT NULL CHECK (trigger IN ('manual', 'auto', 'child')),
  caused_by         INTEGER NOT NULL,
  playbook          TEXT,
  playbook_hash     TEXT,
  session_id        TEXT,
  git_head          TEXT NOT NULL,
  cwd               TEXT NOT NULL,
  argv_json         TEXT NOT NULL CHECK (json_valid(argv_json)),
  selection_json    TEXT CHECK (selection_json IS NULL OR json_valid(selection_json)),
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  outcome           TEXT,
  exit_code         INTEGER,
  duration_seconds  REAL,
  log_path          TEXT,
  PRIMARY KEY (project_id, run_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
-- "which runs are in the air right now", across every task, with no corpus parse
CREATE INDEX ix_run_live ON task_run(project_id, started_at) WHERE ended_at IS NULL;
CREATE INDEX ix_run_task ON task_run(project_id, task_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- History. One row per state-changing event, carrying before AND after.
-- ---------------------------------------------------------------------------

CREATE TABLE task_event (
  event_id       INTEGER PRIMARY KEY,
  project_id     TEXT NOT NULL,
  task_id        TEXT NOT NULL,
  ts             TEXT NOT NULL,
  actor          TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN (
                   'create', 'claim', 'handoff', 'release', 'close', 'reopen',
                   'archive', 'unarchive', 'reprioritize', 'queue_move',
                   'update_content', 'import')),
  log_entry_id   INTEGER,
  operation_id   TEXT,
  -- Bulk renumbering is not activity. The page uses this to keep a 132-file
  -- reorder out of the activity series (analytics-design section 8.7, item H).
  mechanical     INTEGER NOT NULL DEFAULT 0 CHECK (mechanical IN (0, 1)),
  -- 'native' was written by a verb and is exact; 'reconstructed' was replayed
  -- from the log; 'backfilled' came from git and its ts is a bound, not an
  -- observation. This is how "unknown" stays distinguishable from zero.
  source         TEXT NOT NULL DEFAULT 'native'
                 CHECK (source IN ('native', 'reconstructed', 'backfilled')),

  lifecycle_from   TEXT,    lifecycle_to   TEXT,
  ball_from        TEXT,    ball_to        TEXT,
  ball_reason_from TEXT,    ball_reason_to TEXT,
  outcome_from     TEXT,    outcome_to     TEXT,
  archived_from    INTEGER, archived_to    INTEGER,
  priority_from    TEXT,    priority_to    TEXT,
  owner_from       TEXT,    owner_to       TEXT,
  position_from    INTEGER, position_to    INTEGER,
  parent_from      TEXT,    parent_to      TEXT,
  detail_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),

  -- The whole backlog-level chart is a running sum of this one indexed integer.
  open_delta INTEGER NOT NULL GENERATED ALWAYS AS (
    CASE
      WHEN kind = 'create' THEN 1
      WHEN lifecycle_to = 'closed'
           AND (lifecycle_from IS NULL OR lifecycle_from <> 'closed') THEN -1
      WHEN lifecycle_from = 'closed'
           AND lifecycle_to IS NOT NULL AND lifecycle_to <> 'closed' THEN 1
      ELSE 0
    END) STORED,

  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
CREATE INDEX ix_event_project_ts ON task_event(project_id, ts);
CREATE INDEX ix_event_task_ts    ON task_event(project_id, task_id, ts);
CREATE INDEX ix_event_backlog    ON task_event(project_id, ts, open_delta) WHERE open_delta <> 0;
CREATE INDEX ix_event_closed     ON task_event(project_id, ts) WHERE lifecycle_to = 'closed';
CREATE INDEX ix_event_ball       ON task_event(project_id, ts) WHERE ball_to IS NOT ball_from;
-- analytics-design section 6, item F: coverage runs on every page load,
-- 21.9 ms -> 6.58 ms at 20x.
CREATE INDEX ix_event_source     ON task_event(project_id, source, ts);

-- ---------------------------------------------------------------------------
-- Idempotency ledger. Project-scoped rather than task-scoped, so a bare create
-- is idempotent without depending on having written a log entry (audit P3).
-- ---------------------------------------------------------------------------

CREATE TABLE operation (
  project_id      TEXT NOT NULL,
  operation_id    TEXT NOT NULL,
  kind            TEXT NOT NULL,
  actor           TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,
  task_id         TEXT,
  log_entry_id    INTEGER,
  applied_at      TEXT NOT NULL,
  result_revision INTEGER,
  PRIMARY KEY (project_id, operation_id)
);
CREATE INDEX ix_operation_task ON operation(project_id, task_id);

-- ---------------------------------------------------------------------------
-- Webhook outbox. Enqueued in the same transaction as the state change, so a
-- replayed operation enqueues nothing (audit F3) and a crash between commit and
-- send loses no notification (task-047, task-255).
-- ---------------------------------------------------------------------------

CREATE TABLE webhook_outbox (
  id              INTEGER PRIMARY KEY,
  project_id      TEXT NOT NULL,
  task_id         TEXT,
  event           TEXT NOT NULL,
  payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
  created_at      TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  delivered_at    TEXT,
  last_error      TEXT
);
CREATE INDEX ix_outbox_pending ON webhook_outbox(next_attempt_at) WHERE delivered_at IS NULL;

-- ---------------------------------------------------------------------------
-- Quarantine. Malformed legacy records land here, outside every constraint, so
-- an import never has to choose between silence and refusing the corpus.
-- ---------------------------------------------------------------------------

CREATE TABLE import_quarantine (
  id            INTEGER PRIMARY KEY,
  project_id    TEXT NOT NULL,
  source_path   TEXT NOT NULL,
  task_id_guess TEXT,
  raw_text      TEXT NOT NULL,
  error         TEXT NOT NULL,
  imported_at   TEXT NOT NULL,
  resolved_at   TEXT
);
CREATE INDEX ix_quarantine_open ON import_quarantine(project_id) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- Search
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE task_fts USING fts5(
  task_id UNINDEXED,
  project_id UNINDEXED,
  title,
  summary,
  description,
  ball_prompt,
  tags,
  tokenize = 'unicode61'
);
