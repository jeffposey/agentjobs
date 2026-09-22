-- AgentJobs authoritative SQLite store, physical schema version 8.
--
-- Bounded agent loops get their two log entry types (task-150). `chain_authorized` is a
-- human's authorisation of up to N dispatches against one task, carrying the bounds and
-- the digest over the criteria's checks; `chain_revoked` withdraws one.
--
-- **Both are manager-written, and this constraint is the floor under that.** The
-- application refuses them at `add_log_entry` because they are in
-- `MANAGER_WRITTEN_LOG_TYPES`, and the capability gate refuses a run at the dedicated
-- verbs. This is the third lock and the only one that binds a writer that is not the
-- application: a row the store will not hold cannot be read back as an authorisation,
-- whatever put it there. The authorisation *is* the permission -- the driver reads no
-- other evidence -- so it is worth three.
--
-- Two types rather than a `revoked_at` on the first, because the log is append-only. The
-- authorisation stays true about what was agreed; the revocation says when somebody took
-- it back. A reader sees both, in order, which is what auditing a loop next week means.
--
-- The log_entry rebuild below is migration 006's, verbatim in structure, for the reason
-- given there: SQLite cannot alter a CHECK constraint, `attachment`'s foreign key
-- cascades on `DROP TABLE log_entry`, and `PRAGMA legacy_alter_table` does not help. The
-- notes on that migration apply here unchanged and are not repeated; the only differences
-- are the table's working name and the two added types.

CREATE TABLE attachment_pre_008 AS SELECT * FROM attachment;

CREATE TABLE log_entry_008 (
  project_id  TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  entry_id    INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN (
                'note', 'progress', 'transition', 'handoff', 'decision', 'question',
                'answer', 'instruction', 'dispatch', 'dispatch_result', 'queue_move',
                'authorization', 'check_result', 'chain_authorized', 'chain_revoked')),
  body        TEXT,
  re          INTEGER,
  data_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),
  PRIMARY KEY (project_id, task_id, entry_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE,
  FOREIGN KEY (project_id, task_id, re) REFERENCES log_entry_008(project_id, task_id, entry_id)
      DEFERRABLE INITIALLY DEFERRED,
  CHECK (re IS NULL OR re < entry_id)
);

INSERT INTO log_entry_008 (
  project_id, task_id, entry_id, ts, actor, type, body, re, data_json)
  SELECT project_id, task_id, entry_id, ts, actor, type, body, re, data_json
  FROM log_entry;

DROP TRIGGER log_feed_on_insert;
DROP TRIGGER log_feed_on_delete;
DROP TABLE log_entry;

ALTER TABLE log_entry_008 RENAME TO log_entry;

INSERT INTO attachment (project_id, task_id, entry_id, ord, sha256, label)
  SELECT project_id, task_id, entry_id, ord, sha256, label FROM attachment_pre_008;
DROP TABLE attachment_pre_008;

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
