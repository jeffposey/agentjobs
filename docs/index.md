# AgentJobs documentation

AgentJobs is a git-backed handoff protocol with a packaged React web application. Task
YAML remains the source of truth; the React UI at `/app/`, the REST API, CLI, and Python
client are views and controlled writers over the same schema-v2 records.

## Start here

- [Installation](installation.md) — install from a clone today and understand the
  Python-only runtime contract for release wheels.
- [Quick start](quickstart.md) — initialize a project, open the React application,
  create a task, and exercise the canonical handoff loop.
- [Agent workflow](agent-workflow.md) — resume work from a task record and use the
  schema-v2 state verbs.
- [Mobile and installed-app access](mobile-access.md) — privately expose and install
  the React PWA over HTTPS.
- [API reference](api-reference.md) — current endpoint families and the generated
  OpenAPI source of truth.

## Current schema

Schema v2 is implemented in `src/agentjobs/models_v2.py`, declared in
`schema/agentjobs-v2.yaml`, and used by the live task corpus.

- [Understand schema v2](schema/understanding.md)
- [Task schema reference](task-schema.md)
- [v2 entity diagram](schema/v2-erd.md)
- [Generated v2 reference](schema/v2/index.md)
- [Historical design rationale](schema-design.md)

Schema v1 is retired. Its [entity diagram](schema/v1-erd.md) and
[generated reference](schema/v1/index.md) remain only for migration and repository
history; new records and integrations must use v2.

## Development and operations

- [Repository engineering guidance](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md)
- [React frontend development](https://github.com/jeffposey/agentjobs/blob/main/frontend/README.md)
- [Webhook integrations](webhooks.md)
- [Schema migration](migration-guide.md)
- [Agent dispatch design](agent-dispatch-design.md) — accepted design record; clearly
  labelled where implementation is still pending

Everything under `docs/schema/v1/`, `docs/schema/v2/`, and `schema/generated/` is
generated. Regenerate it from the repository root with:

```bash
bash scripts/regen-schema-docs.sh
```
