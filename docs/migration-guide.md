# Schema migration guide

AgentJobs uses schema v2. A v1 task has no `schema: 2` stamp and is deliberately
rejected rather than guessed at load time.

Preview a corpus conversion first:

```bash
poetry run agentjobs migrate-schema --tasks-dir tasks/legacy
```

Write converted files to a separate directory for review:

```bash
poetry run agentjobs migrate-schema \
  --tasks-dir tasks/legacy \
  --output-dir tasks/converted \
  --apply \
  --report migration-report.md
```

The migration is all-or-nothing: if one file cannot be converted without information
loss, nothing is written. Review the report and the converted YAML, then import that
directory:

```bash
poetry run agentjobs storage import --project <id>
```

Task records are rows in a database beside the server, so the converted files are an
input to that import rather than a corpus you commit. Keep them until the import verifies
and you have taken a backup; [the storage guide](storage-sqlite.md#9-importing-an-existing-corpus)
is the sequence, and it runs once, in one direction.

The field mapping and rejected alternatives are recorded in the historical
[schema-v2 design](schema-design.md); the current result is documented in the
[task schema reference](task-schema.md).
