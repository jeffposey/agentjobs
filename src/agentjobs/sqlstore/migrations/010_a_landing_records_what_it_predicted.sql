-- AgentJobs authoritative SQLite store, physical schema version 10.
--
-- A landing records what it predicted (task-586). The dashboard and the task page show a
-- progress bar and an "about N min left" estimate for a scripted finish, derived from the
-- finish and gate history already in this store. An estimate nobody checks drifts without
-- anyone seeing it, so at fixed checkpoints -- each step's start and each gate stage's
-- start -- the server writes down the time remaining it predicted. Once the finish ends
-- `finished`, the actual remaining time at each checkpoint is known, and so is each
-- prediction's error. That error is what the analytics page reports and what the bias
-- factor is learned from.
--
-- `raw_eta_s` is the base model's prediction and `eta_s` is what was shown, after the bias
-- factor then in force (`bias`). Both are kept because the bias is learned from the raw
-- prediction's error: learning from the corrected one would feed the correction back into
-- itself.
--
-- `finish_estimator` holds one row per project at most: the moment its learned correction
-- was last reset. Only finishes that started after it teach the bias factor, so a reset
-- returns the estimate to the uncorrected medians without deleting any history.

CREATE TABLE finish_prediction (
  project_id   TEXT NOT NULL,
  finish_id    TEXT NOT NULL,
  checkpoint   TEXT NOT NULL,
  predicted_at TEXT NOT NULL,
  elapsed_s    REAL NOT NULL,
  raw_eta_s    REAL NOT NULL,
  bias         REAL NOT NULL,
  eta_s        REAL NOT NULL,
  PRIMARY KEY (project_id, finish_id, checkpoint),
  FOREIGN KEY (project_id, finish_id) REFERENCES finish(project_id, finish_id) ON DELETE CASCADE
);

CREATE TABLE finish_estimator (
  project_id TEXT PRIMARY KEY,
  reset_at   TEXT
);
