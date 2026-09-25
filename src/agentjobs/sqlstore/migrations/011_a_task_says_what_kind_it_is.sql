-- AgentJobs authoritative SQLite store, physical schema version 11.
--
-- A task says what kind it is (task-592): `design` or `implementation`, with NULL meaning
-- implementation. Every existing row gets NULL, which is exactly what it meant before the
-- column existed, so no record changes meaning and nothing is backfilled here. The open
-- design passes are marked one at a time through `task_update_content`, each with its own
-- log entry -- a judgement per task, not a rewrite a migration is entitled to make.
--
-- **No CHECK constraint, unlike `posture`.** SQLite cannot alter a CHECK, so one here
-- would turn adding a third kind into a rebuild of the whole task table, and the design
-- (docs/task-kind-design.md section 1) keeps widening this enum cheap on purpose. The
-- model validates every write before it reaches this column, which is the same guard
-- `category` has.

ALTER TABLE task ADD COLUMN kind TEXT;
