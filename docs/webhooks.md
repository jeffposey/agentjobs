# AgentJobs webhook guide

AgentJobs emits HMAC-signed HTTP webhooks for durable schema-v2 workflow events. They
are an integration surface for notifications and automation; they do not replace the
task record or make the React application depend on an external service.

## Supported events

| Event | Trigger |
| --- | --- |
| `task.handoff` | The ball moves to an agent, human, or external dependency |
| `task.question` | A typed `question` entry is appended to the task log |
| `task.closed` | A task closes with an outcome |
| `webhook.test` | The test endpoint probes one subscription |

The older v1 events such as `task.status_changed`, `task.comment_created`, and
`task.completed` are retired. Schema v2 represents those concepts through handoffs,
typed log entries, and close outcomes.

## Payload and signature

Task events include the complete schema-v2 task plus event-specific metadata:

```json
{
  "event": "task.handoff",
  "timestamp": "2026-08-13T20:00:00+00:00",
  "task": {
    "schema": 2,
    "id": "task-123-feature",
    "lifecycle": "active",
    "ball": "human",
    "ball_reason": "review",
    "ball_prompt": "Review the verified branch and approve or request changes."
  },
  "triggered_by": "codex",
  "ball": "human",
  "ball_reason": "review",
  "ball_prompt": "Review the verified branch and approve or request changes."
}
```

The abbreviated task above shows relevant fields; actual deliveries contain the whole
record. Every request carries:

```text
X-Hub-Signature-256: sha256=<hex digest>
```

Validate it against the exact request body with a constant-time comparison:

```python
import hashlib
import hmac

expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
valid = hmac.compare_digest(expected, signature_header)
```

## Manage subscriptions

```bash
curl -X POST http://localhost:8765/api/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:5000/webhook",
    "events": ["task.handoff", "task.question", "task.closed"],
    "secret": "replace-with-a-random-secret"
  }'
```

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/webhooks` | List subscriptions |
| `GET` | `/api/webhooks/{webhook_id}` | Read one subscription |
| `POST` | `/api/webhooks` | Create a subscription |
| `DELETE` | `/api/webhooks/{webhook_id}` | Delete a subscription |
| `POST` | `/api/webhooks/{webhook_id}/test` | Send a `webhook.test` delivery |

Equivalent project-scoped endpoints live under
`/api/projects/{project_id}/webhooks`. Delivery failures are logged and do not block
the state-changing request.
