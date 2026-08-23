# AgentJobs webhook guide

AgentJobs emits HMAC-signed HTTP webhooks for durable schema-v2 workflow events. They
are an integration surface for notifications and automation; they do not replace the
task record or make the React application depend on an external service.

## Supported events

| Event | Trigger | Extra payload keys |
| --- | --- | --- |
| `task.handoff` | The **`handoff` verb** is called | `ball`, `ball_reason`, `ball_prompt` |
| `task.question` | A typed `question` entry is appended to the task log | `body` |
| `task.closed` | A task closes with an outcome | `outcome` |
| `webhook.test` | The test endpoint probes one subscription | — |

**Four events, and only four things fire them.** `task.handoff` is emitted by the
handoff verb specifically, not by every movement of the ball: `claim`, `release` and
`promote` all move it and fire nothing. That is deliberate — those three are an agent
managing its own queue position, and a subscriber that wanted to be woken for them would
be woken constantly — but the distinction is worth knowing before you build a
notification service on this and wonder why a claim was silent.

*(The trigger column previously said "the ball moves to an agent, human, or external
dependency", which describes three of the verbs that do not fire it.)*

Every task event carries `triggered_by` alongside the keys in the last column.

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
