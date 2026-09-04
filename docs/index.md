# AgentJobs documentation

AgentJobs is a git-backed handoff protocol with a packaged React web application. Task
YAML remains the source of truth; the React UI at `/app/`, the REST API, CLI, Python
client, and [MCP server](mcp.md) are views and controlled writers over the same
schema-v2 records.

For agents, the task files are **readable generated state**: read them freely, and make
every change through the [MCP tools](mcp.md), the API, or the CLI, all of which reach
the same validated, locked, logged write path. A direct edit skips all three.

## Start here

- [Installation](installation.md) — install from a clone today and understand the
  Python-only runtime contract for release wheels.
- [Quick start](quickstart.md) — initialize a project, open the React application,
  create a task, and exercise the canonical handoff loop.
- [Agent workflow](agent-workflow.md) — resume work from a task record, use the
  schema-v2 state verbs, and supervise an epic's children as separate sessions.
- [The MCP server](mcp.md) — the managed interface agents should use for every task
  read and write, and exactly what each layer does and does not prevent.
- [Connecting a client](mcp-clients.md) — Codex, Claude, Gemini, and any other MCP
  client, with the protection each one receives.
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
- [Historical design rationale](schema-design.md) — accurate about *why*, out of date about *what*

Schema v1 is retired. Its [entity diagram](schema/v1-erd.md) and
[generated reference](schema/v1/index.md) remain only for migration and repository
history; new records and integrations must use v2.

## Development and operations

- [Repository engineering guidance](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md)
- [React frontend development](https://github.com/jeffposey/agentjobs/blob/main/frontend/README.md)
- [Webhook integrations](webhooks.md)
- [Schema migration](migration-guide.md)
- [Performance](performance.md) — how to measure the API, the CLI, the browser, the
  repository gate and dispatched agent time, what a claim of "faster" has to state, and
  the measurement history behind the gate's quoted costs.
- [Codex dispatch rollout](codex-dispatch.md) — batch runners, the MCP requirement, and
  the rollout sequence.

## Every document here, and what kind of thing it is

A design record is not a manual, and a dated report is neither. Reading one as another
is how this project has lost the most time, so the status word comes first.

**Shipped** — describes behaviour you can rely on today.

| Document | Covers |
| --- | --- |
| [Installation](installation.md) | Installing from a clone or a wheel |
| [Quick start](quickstart.md) | Project init through the first handoff |
| [Agent workflow](agent-workflow.md) | Resuming from a record; the state verbs; supervising children |
| [API reference](api-reference.md) | Every REST operation, and the fact that none is authenticated |
| [The MCP server](mcp.md) | The managed write path and what each layer prevents |
| [Connecting a client](mcp-clients.md) | Per-client configuration and the protection each receives |
| [Mobile and installed-app access](mobile-access.md) | HTTPS, the tailnet, and the PWA |
| [Webhook integrations](webhooks.md) | The four events, signatures, payloads |
| [Task schema reference](task-schema.md) | Every field, enum and consistency rule |
| [Understand schema v2](schema/understanding.md) | The schema explained rather than tabulated |
| [Schema migration](migration-guide.md) | v1 to v2, and the all-or-nothing rule |
| [Performance](performance.md) | The measurement tools and their contract |
| [Codex dispatch rollout](codex-dispatch.md) | The Codex runner setup |
| [Queue position design](task-selection-design.md) | The explicit work order — **accepted 2026-08-20, implemented 2026-08-21** (task-081, 204–209) |
| [Agent dispatch design](agent-dispatch-design.md) | Turning an approval into a running agent — **shipped**; its header lists what landed under which task, and the four things in it that were never built |
| [Playbooks design](playbooks-design.md) | Reusable briefs for recurring judgment work — **partly shipped**: storage and read surfaces (task-214), instantiation and the Run button (task-215). Its header says what remains. |
| [Principals design](principals-design.md) | Who is asking, resolved per request: the trust rule (task-329) and the run credential that splits loopback in two (task-331). Resolution only — what the answer permits is the page below. |
| [Authorization](authorization.md) | What each kind of principal may do: the capability table, the actor-agreement rule, the refusal codes, and the four things it deliberately does not do (task-332). |
| [Exposure](exposure.md) | Which projects a caller is served at all: the `visibility` setting, why a hidden project answers as absent rather than forbidden, and why the transcript routes were the ones that needed it (task-333). |
| [Identity registry](identity-registry.md) | Which configured person a proven login is: several humans per project, the machine-level login map, and retirement (task-330). Attribution, not authorization. |
| [Tailnet front door](tailnet-front-door.md) | The tsnet proxy that proves who a remote caller is, refuses a connection it cannot identify, and denies three routes (task-244). Includes what the tailnet ACL actually allows, read 2026-09-04. |

**Design records — accepted, not yet built.** Read for reasoning, never as a manual.

| Document | State |
| --- | --- |
| [Agent loops design](agent-loops-design.md) | No implementation. Derived tasks are open and unclaimed. |
| [MCP integration design](mcp-integration-design.md) | Implemented, and the record has drifted behind it by two tools and one error code. The reference pages above are what shipped. |

**Historical.** True when written, kept for the reasoning, not maintained.

| Document | Note |
| --- | --- |
| [Schema v2 design rationale](schema-design.md) | Predates `queue_position` and the storage locks; several present-tense claims are no longer true, and its banner says so. |
| [Schema v1 entity diagram](schema/v1-erd.md) and [generated v1 reference](schema/v1/index.md) | v1 is retired. Migration and history only. |
| [The agentjobs package integration note](integration/agentjobs-package.md) | An 11-line stub, superseded. |

**Dated reports.** A measurement of one day, not a standing claim.

| Document | Date |
| --- | --- |
| [Task corpus audit](task-corpus-audit.md) | 2026-08-13 structural audit, plus a 2026-08-27 record-quality baseline — summary lengths by era and how often a `question` entry ever reaches a record. Regenerate that half with `scripts/corpus_stats.py`. |
| [MCP release evidence](integration/mcp-release-evidence.md) | 2026-08-17. Its counts — "fourteen tools", "1089 tests" — were right that day and are not now. |
| [The context budget](context-budget.md) | 2026-08-25. What a session loads before its first thought, per runner, with the instrument behind every figure. The totals move with each harness release and each commit to the always-loaded bundle; the methods and the proposed cap do not. |

**Generated.** Never hand-edit; regenerate.

[v2 entity diagram](schema/v2-erd.md) · [generated v2 reference](schema/v2/index.md) ·
[generated v1 reference](schema/v1/index.md) · everything under `docs/schema/v1/`,
`docs/schema/v2/` and `schema/generated/`.

```bash
bash scripts/regen-schema-docs.sh
```

That script also validates the live task corpus against v2 and exits non-zero on
failure, which makes it a useful check that no stage of `scripts/check.py` runs.
`tests/test_schema_generated_is_current.py` covers the narrower question of whether the
committed JSON Schema still matches its source.
