-- AgentJobs authoritative SQLite store, physical schema version 4.
--
-- A bounded feed of every log entry, in the order the store committed them, with a
-- position that is never reused (task-264).
--
-- The execution journal imports human handoffs, approvals and answers into its inbox
-- from the task log. It has to import *every* entry it has not seen, not only the newest
-- handoff: a process that commits a handoff and dies before telling anyone must still
-- have that handoff found, and two change requests in a row must both arrive. That needs
-- a cursor, and a cursor needs a position that only ever grows.
--
-- `log_entry` has no such column. Its key is per task, and its implicit rowid is not a
-- sequence: SQLite reuses the largest rowid after that row is deleted, and `VACUUM` --
-- which the backup path runs as `VACUUM INTO` -- may renumber rowids of a table without an
-- explicit INTEGER PRIMARY KEY. Either would let a cursor silently step past a new entry.
-- An AUTOINCREMENT key is the one SQLite guarantees is never reused, so the feed is its
-- own table, filled by trigger in the same transaction as the entry it records.
--
-- Existing entries are back-filled in timestamp order, which is the best order the
-- history holds; nothing reads their relative order, only that each has a position.
-- Deleting a task removes its feed rows with it, and never frees a position.

CREATE TABLE log_feed (
  feed_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  entry_id   INTEGER NOT NULL,
  UNIQUE (project_id, task_id, entry_id)
);
CREATE INDEX ix_log_feed_project ON log_feed(project_id, feed_id);

INSERT INTO log_feed(project_id, task_id, entry_id)
  SELECT project_id, task_id, entry_id
  FROM log_entry
  ORDER BY ts, project_id, task_id, entry_id;

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
