-- AgentJobs authoritative SQLite store, physical schema version 5.
--
-- Finish and gate history as rows (task-472, analytics design section 20).
--
-- The scripted finish writes `meta.yaml` and `phases.jsonl` under the home directory's
-- `finishes/`, and the gate writes a receipt the next gate overwrites. Neither is
-- queryable: a page asking how long finishes take, which gate stage costs it, or how
-- often a finish stops red had to parse a directory per request, which the analytics
-- design forbids. These four tables are an index over those files. The files stay and
-- remain the human-readable log; `finish_status.py` reads them, not this.
--
-- Two shapes are deliberate. `gate_run` has no foreign key to `finish` or `task_run`,
-- because an agent-side gate in a worktree records itself before the run it belongs to
-- is a row in this store, and a gate run by hand belongs to nothing. And there is no
-- `runner` column anywhere: `task_run` carries one for the dispatch UI, and no series
-- is split by runner (design section 16, decision 12).
--
-- `source` says whether the row was written by the finisher or the gate as it ran
-- (`native`) or by the one-time import of the directories on disk (`imported`). An
-- imported write never overwrites a row that exists, so the import can be re-run.

CREATE TABLE finish (
  project_id        TEXT NOT NULL,
  finish_id         TEXT NOT NULL,
  task_id           TEXT NOT NULL,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  seconds           REAL,
  outcome           TEXT NOT NULL CHECK (outcome IN
                      ('finished', 'escalated', 'declined', 'interrupted', 'running')),
  reason            TEXT,
  stopped_at        TEXT,
  merged            INTEGER NOT NULL DEFAULT 0 CHECK (merged IN (0, 1)),
  merge_commit      TEXT,
  run_id            TEXT,
  dispatched_run_id TEXT,
  authority         TEXT,
  source            TEXT NOT NULL DEFAULT 'native' CHECK (source IN ('native', 'imported')),
  PRIMARY KEY (project_id, finish_id),
  FOREIGN KEY (project_id, task_id) REFERENCES task(project_id, task_id) ON DELETE CASCADE
);
CREATE INDEX ix_finish_started ON finish(project_id, started_at);
CREATE INDEX ix_finish_task    ON finish(project_id, task_id, started_at DESC);

CREATE TABLE finish_step (
  project_id  TEXT NOT NULL,
  finish_id   TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  step        TEXT NOT NULL,
  ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
  skipped     INTEGER NOT NULL DEFAULT 0 CHECK (skipped IN (0, 1)),
  seconds     REAL NOT NULL DEFAULT 0,
  detail      TEXT,
  ts          TEXT NOT NULL,
  PRIMARY KEY (project_id, finish_id, seq),
  FOREIGN KEY (project_id, finish_id) REFERENCES finish(project_id, finish_id) ON DELETE CASCADE
);

CREATE TABLE gate_run (
  project_id   TEXT NOT NULL,
  gate_id      TEXT NOT NULL,
  origin       TEXT NOT NULL CHECK (origin IN ('finish', 'run', 'manual')),
  finish_id    TEXT,
  run_id       TEXT,
  task_id      TEXT,
  scope        TEXT NOT NULL CHECK (scope IN ('full', 'partial', 'since_gate', 'concurrent')),
  tree         TEXT,
  checkout     TEXT,
  branch       TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  seconds      REAL,
  passed       INTEGER CHECK (passed IN (0, 1)),
  failed_stage TEXT,
  stages_run   INTEGER,
  stages_total INTEGER,
  source       TEXT NOT NULL DEFAULT 'native' CHECK (source IN ('native', 'imported')),
  PRIMARY KEY (project_id, gate_id)
);
CREATE INDEX ix_gate_started ON gate_run(project_id, started_at);
CREATE INDEX ix_gate_task    ON gate_run(project_id, task_id, started_at) WHERE task_id IS NOT NULL;

CREATE TABLE gate_stage (
  project_id  TEXT NOT NULL,
  gate_id     TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  stage       TEXT NOT NULL,
  seconds     REAL,
  passed      INTEGER CHECK (passed IN (0, 1)),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (project_id, gate_id, seq),
  FOREIGN KEY (project_id, gate_id) REFERENCES gate_run(project_id, gate_id) ON DELETE CASCADE
);
CREATE INDEX ix_gate_stage_name ON gate_stage(project_id, stage, started_at);

-- Section 20.5 B, found on the way: the run series filter `task_run` by `started_at`,
-- and `ix_run_live` is partial on `ended_at IS NULL` so it cannot serve them.
CREATE INDEX ix_run_started ON task_run(project_id, started_at);

-- Section 20.5 A: `task_run.started_at` and `ended_at` were stamped with the clock at the
-- moment the dispatch entry was written into the store. For a native dispatch that is the
-- launch to within a second; for the cutover import of 2026-09-07 it was the import, so
-- 171 rows start in the same minute and the run series would draw them all on one day.
-- The dispatch and dispatch_result entries themselves carry the true instants, so the
-- rows are re-stamped from them here. A native row moves by under a second; a run whose
-- entry is missing keeps what it has.
UPDATE task_run
SET started_at = (
  SELECT MIN(e.ts) FROM log_entry e
  WHERE e.project_id = task_run.project_id
    AND e.task_id = task_run.task_id
    AND e.type = 'dispatch'
    AND json_extract(e.data_json, '$.run_id') = task_run.run_id
)
WHERE EXISTS (
  SELECT 1 FROM log_entry e
  WHERE e.project_id = task_run.project_id
    AND e.task_id = task_run.task_id
    AND e.type = 'dispatch'
    AND json_extract(e.data_json, '$.run_id') = task_run.run_id
);

UPDATE task_run
SET ended_at = (
  SELECT MAX(e.ts) FROM log_entry e
  WHERE e.project_id = task_run.project_id
    AND e.task_id = task_run.task_id
    AND e.type = 'dispatch_result'
    AND json_extract(e.data_json, '$.run_id') = task_run.run_id
)
WHERE ended_at IS NOT NULL AND EXISTS (
  SELECT 1 FROM log_entry e
  WHERE e.project_id = task_run.project_id
    AND e.task_id = task_run.task_id
    AND e.type = 'dispatch_result'
    AND json_extract(e.data_json, '$.run_id') = task_run.run_id
);
