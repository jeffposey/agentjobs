-- AgentJobs authoritative SQLite store, physical schema version 7.
--
-- An acceptance criterion gains an executable half (task-147). `check_argv` holds the
-- argv list whose exit code decides `met` or `failed`, as a JSON array of strings, and
-- `check_result` joins the log entry types so a pass over those checks can be recorded.
--
-- **The column is `check_argv`, not `check`.** `CHECK` is a reserved word in SQLite, so
-- a column named that has to be quoted at every site that touches it and an unquoted one
-- is a syntax error far from the schema that caused it. The model's field is still
-- `check`; the mapper in `store.py` is the one place the two names meet.
--
-- NULL means "this criterion has no check", which is the ordinary case and is what every
-- existing row gets. That is deliberately distinct from an empty array, which the model
-- refuses outright: a field left half-written should not read as a decision.
--
-- No existing `verify` value is touched. Turning prose into an argv is a judgement per
-- criterion, not a rewrite a migration is entitled to make.
--
-- The log_entry rebuild below is migration 006's, verbatim in structure, for the reason
-- given there: SQLite cannot alter a CHECK constraint, `attachment`'s foreign key
-- cascades on `DROP TABLE log_entry`, and `PRAGMA legacy_alter_table` does not help. The
-- three notes on that migration apply here unchanged and are not repeated; the only
-- differences are the table's working name and the one added type.

ALTER TABLE task_acceptance ADD COLUMN check_argv TEXT
  CHECK (check_argv IS NULL OR json_valid(check_argv));

CREATE TABLE attachment_pre_007 AS SELECT * FROM attachment;

CREATE TABLE log_entry_007 (
  project_id  TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  entry_id    INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN (
                'note', 'progress', 'transition', 'handoff', 'decision', 'question',
                'answer', 'instruction', 'dispatch', 'dispatch_result', 'queue_move',
                'authorization', 'check_result')),
  body        TEXT,
  re          INTEGER,
  data_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),
  PRIMARY KEY (project_id, task_id, entry_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE,
  FOREIGN KEY (project_id, task_id, re) REFERENCES log_entry_007(project_id, task_id, entry_id)
      DEFERRABLE INITIALLY DEFERRED,
  CHECK (re IS NULL OR re < entry_id)
);

INSERT INTO log_entry_007 (
  project_id, task_id, entry_id, ts, actor, type, body, re, data_json)
  SELECT project_id, task_id, entry_id, ts, actor, type, body, re, data_json
  FROM log_entry;

DROP TRIGGER log_feed_on_insert;
DROP TRIGGER log_feed_on_delete;
DROP TABLE log_entry;

ALTER TABLE log_entry_007 RENAME TO log_entry;

INSERT INTO attachment (project_id, task_id, entry_id, ord, sha256, label)
  SELECT project_id, task_id, entry_id, ord, sha256, label FROM attachment_pre_007;
DROP TABLE attachment_pre_007;

CREATE INDEX ix_log_project_ts ON log_entry(project_id, ts DESC);
CREATE INDEX ix_log_type_ts    ON log_entry(project_id, type, ts DESC);
CREATE INDEX ix_log_thread     ON log_entry(project_id, task_id, re) WHERE re IS NOT NULL;
CREATE INDEX ix_log_questions  ON log_entry(project_id, task_id, entry_id) WHERE type = 'question';

CREATE TRIGGER log_feed_on_insert AFTER INSERT ON log_entry
BEGIN
  INSERT OR IGNORE INTO log_feed(project_id, task_id, entry_id)
  VALUES (NEW.project_id, NEW.task_id, NEW.entry_id);
END;

CREATE TRIGGER log_feed_on_delete AFTER DELETE ON log_entry
BEGIN
  DELETE FROM log_feed
  WHERE project_id = OLD.project_id AND task_id = OLD.task_id AND entry_id = OLD.entry_id;
END;
