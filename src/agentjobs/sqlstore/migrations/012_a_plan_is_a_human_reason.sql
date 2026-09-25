-- AgentJobs authoritative SQLite store, physical schema version 12.
--
-- `plan` joins the human-side ball reasons: a plan, design or approach awaits a
-- go-ahead and nothing is built yet (task-001). `task`'s rule-2b CHECK enumerates the
-- reasons, for the reason migration 006 gives about log entry types: a store whose
-- constraint is narrower than its model refuses a record the model accepts, and one
-- whose constraint is wider can hold a row nothing can read.
--
-- **Not a table rebuild, deliberately.** Migration 006 rebuilt `log_entry` and had to
-- park `attachment` to survive it, because `DROP TABLE` applies ON DELETE CASCADE and
-- `PRAGMA foreign_keys` cannot be switched off inside the transaction every migration
-- runs in. Thirteen tables reference `task` that way. Rebuilding it here would mean
-- parking every one of them, their triggers and their feed positions, to change one
-- IN list.
--
-- The change only *widens* the CHECK: every row that satisfied the old list satisfies
-- the new one, and nothing about the on-disk format moves. That is the case SQLite's
-- ALTER TABLE documentation gives its simpler procedure for ("Making Other Kinds Of
-- Table Schema Changes": removing CHECK constraints) -- edit the table's text in
-- `sqlite_schema` under `writable_schema`, then bump the schema cookie so every other
-- connection re-reads it. The cookie is bumped by the scratch table below, whose
-- CREATE and DROP are DDL; its CHECK is also what fails this migration, rolling it back
-- whole, if the text it looked for was not there and nothing was widened.

PRAGMA writable_schema = ON;

UPDATE sqlite_schema
   SET sql = replace(
         sql,
         '(''spec'', ''review'', ''decision'', ''approval'', ''input'')',
         '(''spec'', ''review'', ''plan'', ''decision'', ''approval'', ''input'')')
 WHERE type = 'table' AND name = 'task';

PRAGMA writable_schema = RESET;

CREATE TABLE migration_012_widened (widened INTEGER NOT NULL CHECK (widened = 1));
INSERT INTO migration_012_widened (widened)
  SELECT count(*) FROM sqlite_schema
   WHERE type = 'table' AND name = 'task'
     AND instr(sql, '''review'', ''plan'', ''decision''') > 0;
DROP TABLE migration_012_widened;
