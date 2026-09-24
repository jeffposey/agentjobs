-- AgentJobs authoritative SQLite store, physical schema version 9.
--
-- A gate records the free physical memory it ran in (task-548). On 2026-09-23 pytest went
-- from about 180s a gate to 1,000-1,900s on a machine that had lost about 30 GB, and the
-- history could say how slow every stage was and nothing about why. These columns are the
-- why: available memory, in MB, at the gate's start and around each stage, the lowest the
-- gate's sampler saw, and whether that fell below the floor the gate judged it against.
--
-- All nullable, and nothing reads them as a precondition: a gate from before this
-- migration, an imported one, and a checkout whose `agentjobs.memory` would not import all
-- write NULL, which means "not measured" rather than zero.

ALTER TABLE gate_run ADD COLUMN free_mb_start INTEGER;
ALTER TABLE gate_run ADD COLUMN free_mb_low INTEGER;
ALTER TABLE gate_run ADD COLUMN low_memory INTEGER CHECK (low_memory IN (0, 1));

ALTER TABLE gate_stage ADD COLUMN free_mb_start INTEGER;
ALTER TABLE gate_stage ADD COLUMN free_mb_end INTEGER;
ALTER TABLE gate_stage ADD COLUMN free_mb_low INTEGER;
ALTER TABLE gate_stage ADD COLUMN low_memory INTEGER CHECK (low_memory IN (0, 1));
