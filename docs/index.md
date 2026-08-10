# AgentJobs Schema

The schema is the product; git is its database. This site is generated from the two
LinkML schemas in [`schema/`](https://github.com/jeffposey/agentjobs/tree/main/schema),
so it cannot drift from them.

| | Status | Source | Verified by |
|---|---|---|---|
| **[v1](schema/v1/index.md)** | Implemented | `src/agentjobs/models.py`, transcribed to `schema/agentjobs-v1.yaml` | `linkml-validate` over all 31 live task files |
| **[v2](schema/v2/index.md)** | Proposed, accepted, **not implemented** | `schema/agentjobs-v2.yaml`, from [the design doc](schema-design.md) | Compiles; consistency rules declared; no corpus exists yet |

Start with the **[v2 entity diagram](schema/v2-erd.md)** and the
**[design rationale](schema-design.md)**, then compare against the
**[v1 entity diagram](schema/v1-erd.md)**.

## What changed, in one screen

v1's single 8-value `status` answered three different questions at once. v2 splits them
into orthogonal axes, which is the change everything else follows from:

| Question | v1 | v2 |
|---|---|---|
| Where in its life? | `status` | `lifecycle` (draft/ready/active/closed) |
| Who acts next? | `status` | `ball` (agent/human/external) — **required while open** |
| Why are they holding it? | `under_review` only | `ball_reason`, scoped to the holder |
| What do they need to do? | — | `ball_prompt` — **required whenever the ball is set** |
| How did it end? | `status` | `outcome` + `archived` as a separate visibility flag |

The two required fields are the point: an open task with nobody responsible, or a
handoff with no stated ask, are both unrepresentable in v2.

Deleted in v2 (D1): `phases`, `prompts`, `issues`, the `Comment` model,
`dependencies[].status`, and `human_summary`. Merged: `status_updates` + `comments` +
`prompts.followups` became one typed append-only `log`.

## Regenerating

```bash
bash scripts/regen-schema-docs.sh   # JSON Schema, ER diagrams, reference docs, corpus validation
poetry run mkdocs serve             # browse at http://127.0.0.1:8000
```

Everything under `docs/schema/v1/`, `docs/schema/v2/`, and `schema/generated/` is
generated. Hand-edit only the two files in `schema/`.

!!! note "On `schema/generated/models_v2_preview.py`"
    `gen-pydantic` renders the v2 schema as working Pydantic models. It is checked in
    as evidence that the schema is implementable and as the starting point for
    task-050 — **not** wired into the package. Whether generated models become the
    real `models.py` or stay a reference is task-050's call.
