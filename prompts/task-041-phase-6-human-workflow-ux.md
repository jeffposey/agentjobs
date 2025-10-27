# Phase 6: Human-Agent Workflow UX

## Context

AgentJobs currently has a critical UX gap: when tasks are in `waiting_for_human` status, humans don't know what to do. There are no action buttons, no clear workflow, and no way to trigger agents automatically once approval is given.

This phase implements GitHub Issues-inspired workflow automation to close the human-agent collaboration loop.

## Problem Statement

### Current Workflow (Broken)
1. Agent completes work → sets task to `waiting_for_human`
2. Human sees task in GUI... but then what?
   - No approve/reject buttons
   - No feedback form
   - Must manually edit YAML or use API to change status
3. Human eventually changes status to `ready`
4. Agent must manually check for new `ready` tasks
5. Rinse and repeat with friction at every step

### Desired Workflow (Seamless)
1. Agent completes work → sets task to `waiting_for_human`
2. Human opens task → sees clear action buttons: "Approve", "Request Changes", "Reject"
3. Human clicks "Approve" → task changes to `ready` → webhook fires
4. Local Codex listener receives webhook → automatically opens VS Code with task
5. Agent (Codex) starts working immediately

## Research: What GitHub Issues Does Well

### 1. Clear UI Actions
- **Assignment**: Dropdown to assign issues to team members
- **Labels**: Quick categorization with colored labels
- **Comment box**: Always visible for feedback
- **State buttons**: "Close issue", "Reopen", explicit state changes
- **@mentions**: Notify specific people

### 2. IssueOps Pattern
GitHub uses issue comments and labels as automation triggers:
```
Comment: "/approve" → GitHub Action runs → Deploys to production
Label: "approved" → Webhook fires → External CI/CD notified
Issue closed → Automation updates → Dependent systems react
```

### 3. Approval Workflows
- **Required reviewers**: Block deployment until N people approve
- **Approval buttons**: Green checkmark for approve, red X for reject
- **Review comments**: Structured feedback with approve/request-changes/comment
- **Status checks**: Show which approvals are pending vs complete

### 4. Webhooks for Automation
- Issue events trigger HTTP webhooks: `opened`, `closed`, `labeled`, `assigned`
- Webhooks POST JSON payload to configured URLs
- HMAC signature verification for security
- Retry logic with exponential backoff

### 5. Mobile & Notifications
- Email notifications on issue changes
- Mobile app with approve/reject buttons
- In-app notifications dashboard
- Subscription preferences per-issue

## Objective

Implement 4 phases to transform AgentJobs into a smooth human-agent collaboration platform:

1. **Human Action Buttons UI** - Obvious, one-click actions for humans
2. **Webhook System** - Fire HTTP webhooks on task events
3. **Comments & Feedback** - Threaded discussion on tasks
4. **Codex Auto-Trigger** - Local listener that auto-opens VS Code when work is ready

## Phase 1: Human Action Buttons UI

### Requirements

When viewing a task with `status = waiting_for_human`:

**Display prominent action panel:**
```html
<div class="human-actions-panel">
  <h3>⚠️ This task needs your review</h3>
  <p>Review the agent's work below and take action:</p>

  <div class="action-buttons">
    <button class="btn-approve">✓ Approve & Make Ready</button>
    <button class="btn-changes">✎ Request Changes</button>
    <button class="btn-reject">✗ Reject Task</button>
  </div>

  <div class="feedback-form" style="display:none;">
    <label>Feedback or questions:</label>
    <textarea id="feedback" placeholder="Explain what needs to change..."></textarea>
    <button class="btn-submit-feedback">Submit</button>
    <button class="btn-cancel">Cancel</button>
  </div>
</div>
```

**Button Behaviors:**

- **Approve**:
  - Changes task status to `ready`
  - Adds status_update: "Approved by {user}"
  - Optionally assigns to agent (if assigned_to field is set)
  - Shows success toast notification
  - Redirects to task list

- **Request Changes**:
  - Shows feedback textarea
  - Keeps status as `waiting_for_human`
  - Adds status_update with feedback
  - Shows toast: "Feedback sent to agent"

- **Reject**:
  - Shows confirmation dialog: "Are you sure? This will archive the task."
  - Changes status to `archived`
  - Adds status_update: "Rejected by {user}: {reason}"
  - Redirects to task list

### Implementation Steps

1. **Update task_detail.html template**
   - Add conditional block: `{% if task.status == 'waiting_for_human' %}`
   - Include action buttons panel
   - Add Alpine.js component for button interactions
   - Style with Tailwind for prominence (orange/yellow theme)

2. **Add API endpoints**
   ```python
   POST /api/tasks/{task_id}/approve
   POST /api/tasks/{task_id}/request-changes
   POST /api/tasks/{task_id}/reject
   ```

3. **Add JavaScript handlers**
   - Click handlers for each button
   - Fetch API calls to endpoints
   - Toast notifications on success/error
   - Form validation for feedback textarea

### Acceptance Criteria

- [ ] When viewing waiting_for_human task, action panel is prominently displayed
- [ ] Clicking "Approve" changes status to ready and adds status_update
- [ ] Clicking "Request Changes" shows feedback form
- [ ] Submitting feedback keeps status as waiting_for_human and records feedback
- [ ] Clicking "Reject" shows confirmation dialog before archiving
- [ ] All actions show toast notifications
- [ ] Panel is NOT shown for tasks in other statuses

## Phase 2: Webhook System

### Requirements

Fire HTTP POST webhooks when task events occur:

**Events to support:**
- `task.created`
- `task.updated`
- `task.status_changed` (most important!)
- `task.assigned`
- `task.completed`
- `task.deleted`

**Webhook payload format:**
```json
{
  "event": "task.status_changed",
  "timestamp": "2025-10-27T10:30:00Z",
  "task": {
    "id": "task-041-phase-6-human-workflow-ux",
    "title": "Phase 6: Human-Agent Workflow UX",
    "status": "ready",
    "previous_status": "waiting_for_human",
    "assigned_to": "Codex",
    "priority": "high",
    ...full task object...
  },
  "triggered_by": "jeff",
  "action": "approved"
}
```

**Security:**
- HMAC-SHA256 signature in `X-Hub-Signature-256` header
- Secret key configured per-webhook
- Signature verification on receiving end

### Implementation Steps

1. **Add Webhook model**
   ```python
   class Webhook(BaseModel):
       id: str
       url: HttpUrl
       events: List[str]  # e.g., ["task.status_changed", "task.created"]
       secret: str  # For HMAC signature
       active: bool = True
       created: datetime
       last_triggered: Optional[datetime] = None
   ```

2. **Add WebhookManager**
   ```python
   class WebhookManager:
       def fire_webhook(self, event: str, task: Task, metadata: dict):
           """Fire all webhooks registered for this event"""
           webhooks = self.get_webhooks_for_event(event)
           for webhook in webhooks:
               payload = self._build_payload(event, task, metadata)
               signature = self._compute_hmac(payload, webhook.secret)
               self._post_webhook(webhook.url, payload, signature)

       def _compute_hmac(self, payload: str, secret: str) -> str:
           """Compute HMAC-SHA256 signature"""
           return hmac.new(
               secret.encode(),
               payload.encode(),
               hashlib.sha256
           ).hexdigest()
   ```

3. **Integrate webhook firing**
   - In `TaskManager.update_status()`: Fire `task.status_changed` webhook
   - In `TaskManager.create_task()`: Fire `task.created` webhook
   - In API endpoints: Pass user context for `triggered_by`

4. **Add webhook management API**
   ```python
   POST /api/webhooks - Create webhook
   GET /api/webhooks - List webhooks
   DELETE /api/webhooks/{id} - Delete webhook
   POST /api/webhooks/{id}/test - Send test ping
   ```

5. **Add webhook management GUI**
   - Settings page with webhook list
   - Form to add new webhook (URL, events, secret)
   - Test button to verify webhook works

### Acceptance Criteria

- [ ] Webhooks can be created via API
- [ ] When task status changes, webhook fires to registered URLs
- [ ] Webhook payload includes full task data and metadata
- [ ] HMAC signature is computed correctly
- [ ] Receiving server can verify signature
- [ ] Webhook firing doesn't block task updates (async)
- [ ] Failed webhooks are logged but don't crash the system

## Phase 3: Comments & Feedback

### Requirements

Add threaded discussion capability to tasks, similar to GitHub Issues comments.

**Comment model:**
```python
class Comment(BaseModel):
    id: str
    task_id: str
    author: str  # username or "Codex"
    content: str  # Markdown supported
    created: datetime
    updated: Optional[datetime] = None
    reply_to: Optional[str] = None  # For threading
```

**Where comments appear:**
- In task detail page, below description
- In status_updates timeline (interleaved)
- In agent prompts (context for Codex)

### Implementation Steps

1. **Add Comment model to models.py**

2. **Add comments array to Task model**
   ```python
   class Task(BaseModel):
       ...
       comments: List[Comment] = Field(default_factory=list)
   ```

3. **Add API endpoints**
   ```python
   POST /api/tasks/{task_id}/comments - Add comment
   GET /api/tasks/{task_id}/comments - List comments
   PUT /api/comments/{comment_id} - Edit comment
   DELETE /api/comments/{comment_id} - Delete comment
   ```

4. **Update task_detail.html template**
   - Add comments section below description
   - Show comment thread with authors/timestamps
   - Add comment form (textarea + submit button)
   - Render markdown in comments
   - Add reply button for threading

5. **Integrate with request-changes workflow**
   - When "Request Changes" clicked, feedback goes into comment
   - Comment is tagged with `type: feedback`
   - Agent sees feedback in next prompt

### Acceptance Criteria

- [ ] Humans can add comments to tasks
- [ ] Comments persist in task YAML
- [ ] Comments display in task detail page with formatting
- [ ] Markdown rendering works in comments
- [ ] "Request Changes" feedback appears as comment
- [ ] Agent prompts include recent comments as context

## Phase 4: Codex Auto-Trigger

### Requirements

Create a local listener script that:
1. Listens for webhooks from AgentJobs
2. When `task.status_changed` to `ready` fires
3. Automatically opens VS Code to that task
4. Optionally triggers Codex extension to start work

### Implementation Steps

1. **Create examples/codex_listener.py**
   ```python
   from flask import Flask, request
   import hmac
   import hashlib
   import subprocess

   app = Flask(__name__)
   SECRET = "your-webhook-secret-here"

   @app.route('/webhook', methods=['POST'])
   def webhook():
       # Verify HMAC signature
       signature = request.headers.get('X-Hub-Signature-256')
       if not verify_signature(request.data, signature):
           return {'error': 'Invalid signature'}, 401

       data = request.json
       event = data['event']
       task = data['task']

       # If task changed to READY and assigned to Codex, trigger
       if event == 'task.status_changed':
           if task['status'] == 'ready' and task.get('assigned_to') == 'Codex':
               trigger_codex(task)

       return {'status': 'ok'}

   def trigger_codex(task):
       """Open VS Code and load task"""
       task_file = f"tasks/agentjobs/{task['id']}.yaml"

       # Open VS Code to task file
       subprocess.run(['code', task_file])

       # Optional: Send to Claude Code extension
       # (requires VS Code API integration)

   def verify_signature(payload, signature_header):
       """Verify HMAC-SHA256 signature"""
       expected = 'sha256=' + hmac.new(
           SECRET.encode(),
           payload,
           hashlib.sha256
       ).hexdigest()
       return hmac.compare_digest(expected, signature_header)

   if __name__ == '__main__':
       app.run(port=5000)
   ```

2. **Create docs/webhooks.md**
   - Document webhook system
   - Explain how to run codex_listener.py
   - Show how to configure webhook in AgentJobs
   - Provide example HMAC verification code in multiple languages

3. **Add webhook config to GUI**
   - Settings page: Add webhook URL field
   - Default to `http://localhost:5000/webhook` for local development
   - Show webhook secret (generate random on first setup)
   - Test button to send ping

### Acceptance Criteria

- [ ] codex_listener.py runs locally without errors
- [ ] Listener receives webhook when task status changes
- [ ] HMAC signature verification works
- [ ] VS Code opens to task file when webhook received
- [ ] Documentation explains setup process clearly
- [ ] Example works on Windows/Mac/Linux

## Deliverables

1. **src/agentjobs/api/templates/task_detail.html** - Updated with action buttons
2. **src/agentjobs/api/routes/tasks.py** - New endpoints: approve, request-changes, reject
3. **src/agentjobs/models.py** - Webhook and Comment models
4. **src/agentjobs/webhooks.py** - WebhookManager class
5. **src/agentjobs/api/routes/webhooks.py** - Webhook management API
6. **examples/codex_listener.py** - Local webhook listener
7. **docs/webhooks.md** - Webhook system documentation
8. **tests/test_webhooks.py** - Webhook system tests

## Testing

### Manual Testing Workflow

1. Create test task in `waiting_for_human` status
2. Open in GUI → verify action buttons appear
3. Click "Approve" → verify status changes to `ready`
4. Start codex_listener.py locally
5. Configure webhook to point to `http://localhost:5000/webhook`
6. Approve another task → verify webhook fires
7. Verify VS Code opens automatically

### Automated Tests

```python
def test_approve_button_changes_status():
    """Clicking approve changes status to ready"""
    task = create_test_task(status=TaskStatus.WAITING_FOR_HUMAN)
    response = client.post(f"/api/tasks/{task.id}/approve")
    assert response.status_code == 200
    updated = client.get(f"/api/tasks/{task.id}").json()
    assert updated['status'] == 'ready'

def test_webhook_fires_on_status_change():
    """Webhook fires when task status changes"""
    webhook = create_webhook(url="http://example.com/hook", events=["task.status_changed"])
    with mock.patch('httpx.post') as mock_post:
        manager.update_status(task.id, status=TaskStatus.READY)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert 'X-Hub-Signature-256' in call_args.kwargs['headers']
```

## Success Criteria

- [ ] Humans can approve/reject tasks with one click
- [ ] Webhooks fire on task status changes
- [ ] Local Codex listener receives webhooks and opens VS Code
- [ ] Comments can be added for feedback
- [ ] Request-changes workflow stores feedback as comments
- [ ] Documentation explains how to set up local automation
- [ ] All tests pass

## References

- [GitHub Issues Documentation](https://docs.github.com/en/issues)
- [GitHub Webhooks Documentation](https://docs.github.com/en/webhooks)
- [IssueOps Blog Post](https://github.blog/engineering/issueops-automate-ci-cd-and-more-with-github-issues-and-actions/)
- [HMAC Signature Verification](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

## Notes

- This phase is **critical** for making AgentJobs actually usable in practice
- Without this, the human-agent loop has too much friction
- The webhook system enables future integrations (Slack, email, etc.)
- Comments system enables richer collaboration beyond status changes
