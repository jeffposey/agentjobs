-- AgentJobs authoritative SQLite store, physical schema version 3.
--
-- `task.seq` is the number an id carries, and `SqlTaskStore.generate_task_id` takes the
-- next id from `MAX(seq)`. Its docstring said the column exists so that a slugged id
-- like `task-047-lint-debt` participates in that maximum instead of being skipped the
-- way the file backend's glob skipped it. The parser did not do that: it read the
-- segment after the *last* hyphen, which is `debt`, so every slugged id was stored with
-- `seq = NULL` and the intended fix was never in effect.
--
-- Observed on 2026-09-08 (task-378). Four projects whose ids all carry slugs were cut
-- over, and the next `agentjobs create` in each minted `task-001` beside a
-- `task-001-<slug>` that had been there for weeks -- and would have gone on restarting
-- from 1 on every create. In this repository's own project 158 of 391 rows held NULL
-- for the same reason; the numbering there happens to be safe only because its newest
-- ids carry no slug.
--
-- Fixing the parser fixes rows written after it and nothing already stored, so the
-- column is recomputed here. This is a data repair rather than a shape change: no table
-- is altered, and re-running it on an already-correct database is a no-op.
--
-- The parse, stated in SQL:
--
--   * everything after the first hyphen                        `task-047-lint-debt` -> `047-lint-debt`
--   * up to the next hyphen, or all of it when there is none    -> `047`
--   * NULL unless every character of that is a digit            -> 47
--
-- An id with no hyphen, or whose second segment is not a number, keeps NULL -- which is
-- what "this id carries no number" has always meant here.

UPDATE task
SET seq = CASE
  WHEN instr(task_id, '-') = 0 THEN NULL
  ELSE (
    WITH parsed(number) AS (
      SELECT CASE
        WHEN instr(substr(task_id, instr(task_id, '-') + 1), '-') = 0
          THEN substr(task_id, instr(task_id, '-') + 1)
        ELSE substr(
          task_id,
          instr(task_id, '-') + 1,
          instr(substr(task_id, instr(task_id, '-') + 1), '-') - 1
        )
      END
    )
    SELECT CASE
      WHEN number <> '' AND number NOT GLOB '*[^0-9]*' THEN CAST(number AS INTEGER)
      ELSE NULL
    END
    FROM parsed
  )
END;
