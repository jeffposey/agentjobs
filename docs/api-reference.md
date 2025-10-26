# AgentJobs REST API Reference

The AgentJobs REST API exposes task management capabilities for agents and tooling. All
endpoints serve and accept JSON unless explicitly noted. FastAPI automatically publishes
interactive documentation at [`/docs`](http://localhost:8765/docs).

## Base URL

```
http://localhost:8765
```

No authentication is required during Phase 2. Future releases will add auth headers.

Set the tasks directory explicitly by exporting `AGENTJOBS_TASKS_DIR` (defaults to the
`tasks/` directory configured during `agentjobs init`).

## Conventions

- Timestamps follow ISO 8601 (`2025-01-01T12:00:00Z`).
- Task identifiers take the form `task-###`.
- Enum fields use lowercase strings (`planned`, `in_progress`, `high`, ...).

## Task Endpoints

### List Tasks

`GET /api/tasks`

Query parameters:

- `status_filter` (`planned | in_progress | blocked | under_review | completed | archived`)
- `priority_filter` (`low | medium | high | critical`)

Response (`200 OK`):

```json
[
  {
    "id": "task-101",
    "title": "Implement REST layer",
    "status": "in_progress",
    "priority": "high",
    "category": "engineering",
    "description": "Build FastAPI routes for task management.",
    "prompts": {"starter": "...", "followups": []},
    "status_updates": [...],
    "deliverables": [...]
  }
]
```

### Get Next Task

`GET /api/tasks/next`

Optional query parameter `priority` (e.g. `critical`) filters by priority tier. Returns
`null` when no planned tasks remain.

### Get Task

`GET /api/tasks/{task_id}`

Errors:

- `404` – `{ "detail": "Task task-999 not found" }`

### Create Task

`POST /api/tasks`

Request body:

```json
{
  "title": "Design API contract",
  "description": "Document request/response pairs",
  "priority": "high",
  "category": "engineering",
  "tags": ["api", "docs"]
}
```

Response (`201 Created`) returns the persisted task. Validation errors emit
`400` with a descriptive `detail` string.

### Replace Task

`PUT /api/tasks/{task_id}`

Accepts the same payload shape as creation. Any omitted required fields fall back to the
current task definition.

### Update Task (Partial)

`PATCH /api/tasks/{task_id}`

Body contains only the fields to change. Sending an empty body results in `400`.

### Archive Task

`DELETE /api/tasks/{task_id}`

Sets status to `archived` and records a status update.

### Mark Deliverable Complete

`PATCH /api/tasks/{task_id}/deliverables/{deliverable_path}`

URL-encode the deliverable path (`docs%2Fplan.md`). On success returns the updated task.

## Status & Progress

### Update Status

`POST /api/tasks/{task_id}/status`

```json
{
  "status": "in_progress",
  "author": "codex",
  "summary": "Started work",
  "details": "Initial scaffolding in place."
}
```

### Add Progress Update

`POST /api/tasks/{task_id}/progress`

```json
{
  "author": "codex",
  "summary": "Halfway",
  "details": "Finished REST endpoints"
}
```

Both endpoints return the updated task. Missing tasks return `404`.

## Prompt Management

### Get Starter Prompt

`GET /api/tasks/{task_id}/prompts/starter`

Response:

```json
{"task_id": "task-123", "starter": "Kick-off instructions"}
```

### Add Follow-up Prompt

`POST /api/tasks/{task_id}/prompts`

```json
{
  "author": "human",
  "content": "Clarify error handling scenarios?",
  "context": "QA review"
}
```

Returns the updated task with appended `prompts.followups` entry.

## Search

`GET /api/search?q=keyword`

Searches titles, descriptions, and tags. Blank queries receive `400`.

## Health Check

- `GET /health` – legacy root health endpoint.
- `GET /api/health` – API-scoped health response.

Both return `{ "status": "ok" }`.
