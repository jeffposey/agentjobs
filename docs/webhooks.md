# AgentJobs Webhook Guide

AgentJobs emits HTTP webhooks so external systems (Slack bots, Codex listeners, CI jobs) can react instantly when task state changes. This guide covers configuration, payload format, and local development tips.

## Supported Events

| Event | Trigger |
| --- | --- |
| `task.created` | New task saved for the first time |
| `task.updated` | Any status note, feedback, or metadata update |
| `task.status_changed` | Status value changed (`waiting_for_human → ready`, etc.) |
| `task.comment_created` | Human or agent adds a comment |
| `task.comment_updated` | Comment content edited |
| `task.comment_deleted` | Comment deleted |
| `task.completed` | Status updated to `completed` |
| `task.deleted` | Status updated to `archived` |

Webhooks never block the user interface. They are dispatched asynchronously and failed deliveries are logged for inspection.

## Payload Structure

```json
{
  "event": "task.status_changed",
  "timestamp": "2025-10-27T10:30:00.000123+00:00",
  "task": {
    "id": "task-041-phase-6-human-workflow-ux",
    "title": "Phase 6: Human-Agent Workflow UX",
    "status": "ready",
    "priority": "high",
    "assigned_to": "Codex",
    "status_updates": [
      {
        "timestamp": "2025-10-27T10:30:00.000123+00:00",
        "author": "jeff",
        "message": "Approved by jeff",
        "action": "approve"
      }
    ],
    "comments": [],
    "metadata": {}
  },
  "triggered_by": "jeff",
  "action": "approve",
  "previous_status": "waiting_for_human"
}
```

Every request includes an HMAC-SHA256 signature, using the webhook's secret:

```
X-Hub-Signature-256: sha256=7c813d2c1d61c6f4d4df9f1df41a60a1e0acf5d0846c72769ca66b0e3f62e3ab
```

On the receiving side, compute the same signature and use `hmac.compare_digest` to validate authenticity.

```python
expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
hmac.compare_digest(expected, signature_header)
```

## Managing Webhooks

Use the REST API to manage webhook subscriptions:

- `GET /api/webhooks` — list existing webhooks
- `POST /api/webhooks` — add a new webhook
- `DELETE /api/webhooks/{id}` — remove a webhook
- `POST /api/webhooks/{id}/test` — send a `webhook.test` ping

Example create request:

```bash
curl -X POST http://localhost:8765/api/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:5000/webhook",
    "events": ["task.status_changed"],
    "secret": "super-secret"
  }'
```

## Local Codex Listener

The repository ships with `examples/codex_listener.py`, a minimal Flask server that listens for `task.status_changed` events and launches VS Code when a `ready` task is assigned to Codex.

```bash
poetry install --with dev
export WEBHOOK_SECRET=super-secret
python examples/codex_listener.py
```

Configure AgentJobs to point at `http://localhost:5000/webhook` with the same secret. When a human approves work, the listener validates the signature, prints the payload, and opens the corresponding YAML task file in VS Code.

## Next Steps

Once webhooks are running locally you can expand integrations:

1. Broadcast Slack notifications with feedback summaries.
2. Trigger additional automation (e.g., re-run regression tests) when tasks flip to `ready`.
3. Extend the listener to pass comments into Codex prompts so the agent starts with full human context.

Keep shared workflow expectations aligned by reviewing [`ENGINEERING.md`](ENGINEERING.md) and [`ALLAGENTS.md`](ALLAGENTS.md) when adding new automation hooks.
