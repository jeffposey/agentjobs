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
loss, nothing is written. Review the report and converted YAML, run the repository or
project validation, and only then replace the old corpus through normal version-control
changes. Keep a commit boundary around the migration so the original records remain
recoverable.

The field mapping and rejected alternatives are recorded in the historical
[schema-v2 design](schema-design.md); the current result is documented in the
[task schema reference](task-schema.md).
