-- AgentJobs authoritative SQLite store, physical schema version 13.
--
-- The four postures become the one choice a person makes at dispatch (task-602):
-- `review` (stop for human review) or `automerge` (merge on a green gate).
--
-- **`auto` meant review.** Every row written before this migration stores `auto` for a
-- run that stopped for review, so the mapping is: `auto`, `supervised`, `read_only` ->
-- `review`; `autonomous` -> `automerge`. No new value is spelled `auto`, which is what
-- makes rewriting in place safe: an old row read by old code and a new row read by new
-- code can never agree on a word and disagree on its meaning.
--
-- `task.posture` carries a CHECK naming the four old values, which SQLite cannot alter,
-- so the column is replaced rather than renamed. The new one has no CHECK, for the
-- reason migration 011 gives for `kind`: the model validates every write.
--
-- `task_run.posture_ceiling` was the project's `max_posture`; the ceiling is now a
-- yes/no, `allow_automerge`, and only `autonomous` ever allowed a run to merge itself.
--
-- Dispatch entries' `data_json` carries the same keys (the run row is authoritative and
-- is merged over it on read, but an entry with no run row is read as stored). They are
-- rewritten to the new keys with the same mapping. Readers still accept the old keys --
-- a run directory or an exported record is not reached by this file.

ALTER TABLE task ADD COLUMN merge_mode TEXT;
UPDATE task SET merge_mode =
  CASE posture WHEN 'autonomous' THEN 'automerge' ELSE 'review' END
  WHERE posture IS NOT NULL;
ALTER TABLE task DROP COLUMN posture;

ALTER TABLE task_run RENAME COLUMN posture TO merge_mode;
UPDATE task_run SET merge_mode =
  CASE merge_mode WHEN 'autonomous' THEN 'automerge' WHEN 'automerge' THEN 'automerge'
                  ELSE 'review' END;
ALTER TABLE task_run RENAME COLUMN posture_source TO merge_mode_source;
ALTER TABLE task_run RENAME COLUMN posture_requested TO merge_mode_requested;
UPDATE task_run SET merge_mode_requested =
  CASE merge_mode_requested WHEN 'autonomous' THEN 'automerge' ELSE 'review' END
  WHERE merge_mode_requested IS NOT NULL;
ALTER TABLE task_run ADD COLUMN allow_automerge INTEGER
  CHECK (allow_automerge IS NULL OR allow_automerge IN (0, 1));
UPDATE task_run SET allow_automerge =
  CASE posture_ceiling WHEN 'autonomous' THEN 1 ELSE 0 END
  WHERE posture_ceiling IS NOT NULL;
ALTER TABLE task_run DROP COLUMN posture_ceiling;

-- Dispatch entries: `posture` -> `merge_mode` (mapped), `posture_source` ->
-- `merge_mode_source`, `posture_requested` -> `merge_mode_requested` (mapped),
-- `posture_ceiling` -> `allow_automerge` (boolean), `delivery.posture_delivered` ->
-- `delivery.merge_mode_delivered`. One statement per key, each guarded by the key's
-- presence, so an entry missing some of them keeps exactly the ones it had.
UPDATE log_entry SET data_json = json_remove(
    json_set(data_json, '$.merge_mode',
      CASE json_extract(data_json, '$.posture') WHEN 'autonomous' THEN 'automerge'
                                               ELSE 'review' END),
    '$.posture')
  WHERE type = 'dispatch' AND json_type(data_json, '$.posture') IS NOT NULL;
UPDATE log_entry SET data_json = json_remove(
    json_set(data_json, '$.merge_mode_source', json_extract(data_json, '$.posture_source')),
    '$.posture_source')
  WHERE type = 'dispatch' AND json_type(data_json, '$.posture_source') IS NOT NULL;
UPDATE log_entry SET data_json = json_remove(
    json_set(data_json, '$.merge_mode_requested',
      CASE json_extract(data_json, '$.posture_requested') WHEN 'autonomous' THEN 'automerge'
                                                         ELSE 'review' END),
    '$.posture_requested')
  WHERE type = 'dispatch' AND json_type(data_json, '$.posture_requested') IS NOT NULL;
UPDATE log_entry SET data_json = json_remove(
    json_set(data_json, '$.allow_automerge',
      json(CASE json_extract(data_json, '$.posture_ceiling') WHEN 'autonomous' THEN 'true'
                                                            ELSE 'false' END)),
    '$.posture_ceiling')
  WHERE type = 'dispatch' AND json_type(data_json, '$.posture_ceiling') IS NOT NULL;
UPDATE log_entry SET data_json = json_remove(
    json_set(data_json, '$.delivery.merge_mode_delivered',
      json_extract(data_json, '$.delivery.posture_delivered')),
    '$.delivery.posture_delivered')
  WHERE type = 'dispatch'
    AND json_type(data_json, '$.delivery.posture_delivered') IS NOT NULL;
