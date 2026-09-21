-- AgentJobs authoritative SQLite store, physical schema version 6.
--
-- `authorization` joins the log entry types: a human's authorisation of a dispatch,
-- relayed by the agent they said it to, with the agent in `actor` and the human in
-- `data_json.authorized_by` (task-506).
--
-- The `type` column's CHECK enumerates the types, so a new one needs a migration. That
-- duplication is deliberate and is kept: the model refuses a bad type at every write
-- path, and this refuses one that reached the file some other way -- a hand-edit, an
-- importer, a build of AgentJobs that predates or postdates this one. A store whose
-- constraint is wider than its model is a store that can hold a row nothing can read.
--
-- SQLite cannot alter a CHECK, so the table is rebuilt. Three things about the rebuild
-- are not obvious and each was measured rather than assumed:
--
-- 1.  **`attachment` rows have to be parked.** Its foreign key into `log_entry` is
--     ON DELETE CASCADE, and `DROP TABLE log_entry` applies foreign key actions --
--     `PRAGMA foreign_keys` is ON here and is a no-op inside a transaction, so it cannot
--     be turned off for the duration. Without the parking table the attachments of every
--     log entry in the store are silently deleted by this migration.
-- 2.  **`PRAGMA legacy_alter_table` does not avoid that.** Tried first, on the reading
--     that it leaves references alone: renaming `log_entry` out of the way rewrote
--     `attachment`'s foreign key to name the renamed table, and the cascade followed the
--     rename. So the new table is built under its own name and renamed in, rather than
--     the old one being renamed out.
-- 3.  **The self-reference names `log_entry_006` on purpose.** The rename below is a
--     *non*-legacy one, which rewrites references into the renamed table -- so writing
--     the new name here is what makes the finished table reference `log_entry`. Writing
--     `log_entry` would have pointed the constraint at the table being dropped.
--
-- `log_feed` is untouched and its positions are preserved, which is the property that
-- table exists for: a cursor that has seen position N must never be shown an earlier
-- entry as N+1. The insert trigger is dropped before nothing is copied through it and
-- re-created after, and the existing feed rows keep the positions they already had.

CREATE TABLE attachment_pre_006 AS SELECT * FROM attachment;

CREATE TABLE log_entry_006 (
  project_id  TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  entry_id    INTEGER NOT NULL,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN (
                'note', 'progress', 'transition', 'handoff', 'decision', 'question',
                'answer', 'instruction', 'dispatch', 'dispatch_result', 'queue_move',
                'authorization')),
  body        TEXT,
  re          INTEGER,
  data_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),
  PRIMARY KEY (project_id, task_id, entry_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE,
  FOREIGN KEY (project_id, task_id, re) REFERENCES log_entry_006(project_id, task_id, entry_id)
      DEFERRABLE INITIALLY DEFERRED,
  CHECK (re IS NULL OR re < entry_id)
);

INSERT INTO log_entry_006 (
  project_id, task_id, entry_id, ts, actor, type, body, re, data_json)
  SELECT project_id, task_id, entry_id, ts, actor, type, body, re, data_json
  FROM log_entry;

DROP TRIGGER log_feed_on_insert;
DROP TRIGGER log_feed_on_delete;
DROP TABLE log_entry;

ALTER TABLE log_entry_006 RENAME TO log_entry;

INSERT INTO attachment (project_id, task_id, entry_id, ord, sha256, label)
  SELECT project_id, task_id, entry_id, ord, sha256, label FROM attachment_pre_006;
DROP TABLE attachment_pre_006;

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
