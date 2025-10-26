# Task 031 - Phase 4.2: Human-Agent UX & Waiting Status

---
## ⚠️ CRITICAL: WORKING DIRECTORY ⚠️

**YOU MUST WORK IN THE AGENTJOBS REPOSITORY:**
```bash
cd /mnt/c/projects/agentjobs
```

**DO NOT work in privateproject.**

---

## Problem

Phase 4.1 fixed markdown rendering, but the UI still has critical UX issues:

1. **Information overload** - Task descriptions show verbose agent implementation instructions instead of concise human summaries
2. **No human workflow** - Missing "waiting for human" status and notification system
3. **No prioritization** - Dashboard doesn't spotlight tasks needing human attention
4. **Mixed concerns** - Human context and agent instructions are blended together

**Example from screenshot:**
The description shows detailed implementation steps like "Update `src/agentjobs/api/templates/base.html`" - this is agent context, not human context.

---

## Objectives

1. Add WAITING_FOR_HUMAN status to task workflow
2. Create dashboard spotlight section for tasks needing human attention
3. Add notification badge to nav bar showing count of waiting tasks
4. Separate human summary from agent instructions in data model
5. Add tabbed view in task detail (Human View vs Agent View)
6. Improve migration parser to extract human vs agent content

---

## Implementation

### 1. Extend TaskStatus Enum

Add new status in `src/agentjobs/models.py`:

```python
class TaskStatus(str, Enum):
    """High-level workflow status for tasks."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    WAITING_FOR_HUMAN = "waiting_for_human"  # NEW
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"
    ARCHIVED = "archived"
```

### 2. Enhance Task Model with Human Summary

Add summary field to `src/agentjobs/models.py` Task model (around line 200+):

```python
class Task(BaseModel):
    """Core task entity tracked by AgentJobs."""

    # ... existing fields ...

    human_summary: Optional[str] = Field(
        default=None,
        description="Concise 1-2 sentence summary for human reviewers"
    )
```

### 3. Update Migration Parser

Enhance `src/agentjobs/migration/parser.py` to extract human summaries:

```python
def _extract_human_summary(self, content: str) -> str:
    """Extract concise human-readable summary from task content."""
    # Look for explicit summary sections
    summary_patterns = [
        r"## Summary\s*\n([^\n#]+)",
        r"## Overview\s*\n([^\n#]+)",
        r"## Problem\s*\n([^\n#]+)",
    ]

    for pattern in summary_patterns:
        match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        if match:
            summary = match.group(1).strip()
            # Take first 2 sentences
            sentences = re.split(r'[.!?]\s+', summary)
            return '. '.join(sentences[:2]) + '.'

    # Fallback: extract from description, limit to 200 chars
    desc = self._extract_section(content, ["Objective", "Description"])
    if desc:
        # Remove markdown formatting
        clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', desc)
        clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)
        clean = re.sub(r'`([^`]+)`', r'\1', clean)
        # First sentence or 200 chars
        sentences = re.split(r'[.!?]\s+', clean)
        if sentences:
            summary = sentences[0] + '.'
            return summary[:200] + '...' if len(summary) > 200 else summary

    return "No summary available"

def parse_file(self, file_path: Path) -> ParsedTask:
    # ... existing code ...

    # Add human summary extraction
    human_summary = self._extract_human_summary(content)

    parsed = ParsedTask(
        title=title,
        source_file=file_path,
        raw_content=content,
        description=description,
        human_summary=human_summary,  # NEW
        # ... rest of fields ...
    )
    return parsed
```

Add `human_summary` field to ParsedTask dataclass:

```python
@dataclass
class ParsedTask:
    """Intermediate representation of parsed markdown task."""

    # ... existing fields ...
    human_summary: str = ""  # NEW
```

Update converter to pass human_summary to Task model:

```python
# In src/agentjobs/migration/converter.py
def convert_to_task(self, parsed: ParsedTask) -> Task:
    # ... existing code ...

    task_data = {
        # ... existing fields ...
        "human_summary": parsed.human_summary or parsed.description[:200],
    }
    return Task.model_validate(task_data)
```

### 4. Add Notification Badge to Nav Bar

Update `src/agentjobs/api/templates/base.html` nav section:

```html
<div class="ml-10 flex items-center space-x-4">
    <a href="/" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border transition">
        Dashboard
    </a>
    <a href="/tasks" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border transition relative">
        Tasks
        {% if waiting_count and waiting_count > 0 %}
        <span class="absolute -top-1 -right-1 bg-red-500 text-white text-xs font-bold rounded-full h-5 w-5 flex items-center justify-center">
            {{ waiting_count if waiting_count < 10 else '9+' }}
        </span>
        {% endif %}
    </a>
    <a href="/docs" target="_blank" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border transition">
        API Docs
    </a>
</div>
```

### 5. Add Waiting Tasks Count to Context

Update web route handler in `src/agentjobs/api/routes/web.py`:

```python
from agentjobs.models import TaskStatus

# Add helper function
def get_waiting_count(manager: TaskManager) -> int:
    """Count tasks waiting for human attention."""
    waiting_tasks = manager.list_tasks(status=TaskStatus.WAITING_FOR_HUMAN)
    return len(waiting_tasks)

# Update each route to include waiting_count
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    manager: TaskManager = Depends(get_task_manager),
):
    # ... existing stats logic ...

    waiting_count = get_waiting_count(manager)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "active_tasks": active_tasks,
            "recent_updates": recent_updates,
            "waiting_count": waiting_count,  # NEW
        },
    )

# Do the same for other routes (/tasks, /tasks/{task_id})
```

### 6. Add Dashboard Spotlight Section

Update `src/agentjobs/api/templates/dashboard.html` to add spotlight at top:

```html
{% extends "base.html" %}

{% block title %}Dashboard - AgentJobs{% endblock %}

{% block content %}
<div class="space-y-6">
    <!-- NEW: Waiting for Human Spotlight -->
    {% if waiting_tasks %}
    <div class="bg-gradient-to-r from-red-900/20 to-orange-900/20 border-2 border-red-500/50 rounded-lg p-6">
        <div class="flex items-start gap-4">
            <div class="text-4xl">🔔</div>
            <div class="flex-1">
                <h2 class="text-xl font-bold text-red-300 mb-2">
                    {{ waiting_tasks|length }} {{ 'Task' if waiting_tasks|length == 1 else 'Tasks' }} Waiting for Your Review
                </h2>
                <p class="text-dark-muted mb-4">These tasks need human input to proceed</p>
                <div class="space-y-2">
                    {% for task in waiting_tasks[:3] %}
                    <a href="/tasks/{{ task.id }}" class="block bg-dark-surface hover:bg-dark-border border border-dark-border rounded-lg p-4 transition">
                        <div class="flex items-start justify-between gap-4">
                            <div class="flex-1">
                                <h3 class="font-medium text-dark-text">{{ task.title }}</h3>
                                <p class="text-sm text-dark-muted mt-1">{{ task.human_summary or task.description[:150] }}</p>
                            </div>
                            <span class="px-2 py-1 bg-red-900 text-red-200 text-xs rounded whitespace-nowrap">
                                Waiting
                            </span>
                        </div>
                    </a>
                    {% endfor %}
                    {% if waiting_tasks|length > 3 %}
                    <a href="/tasks?status=waiting_for_human" class="block text-center text-sm text-blue-400 hover:text-blue-300 pt-2">
                        View all {{ waiting_tasks|length }} waiting tasks →
                    </a>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <!-- Existing stats cards -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
        <!-- NEW: Add waiting_for_human stat -->
        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">Waiting for Human</div>
            <div class="text-3xl font-bold mt-2 text-orange-400">{{ stats.waiting_for_human or 0 }}</div>
        </div>

        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">In Progress</div>
            <div class="text-3xl font-bold mt-2">{{ stats.in_progress }}</div>
        </div>
        <!-- ... rest of stats ... -->
    </div>

    <!-- Rest of dashboard ... -->
</div>
{% endblock %}
```

Update route handler to pass `waiting_tasks`:

```python
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    manager: TaskManager = Depends(get_task_manager),
):
    tasks = manager.list_tasks()

    # Calculate stats
    stats = {
        "total": len(tasks),
        "in_progress": sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS),
        "blocked": sum(1 for t in tasks if t.status == TaskStatus.BLOCKED),
        "waiting_for_human": sum(1 for t in tasks if t.status == TaskStatus.WAITING_FOR_HUMAN),  # NEW
        "completed": sum(1 for t in tasks if t.status == TaskStatus.COMPLETED),
    }

    waiting_tasks = [t for t in tasks if t.status == TaskStatus.WAITING_FOR_HUMAN]  # NEW

    # ... existing active_tasks and recent_updates logic ...

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "waiting_tasks": waiting_tasks,  # NEW
            "waiting_count": len(waiting_tasks),  # NEW
            "active_tasks": active_tasks,
            "recent_updates": recent_updates,
        },
    )
```

### 7. Add Human/Agent Tabbed View in Task Detail

Update `src/agentjobs/api/templates/task_detail.html`:

```html
<!-- Replace description section with tabbed view -->
<div class="bg-dark-surface rounded-lg border border-dark-border" x-data="{ tab: 'human' }">
    <div class="border-b border-dark-border">
        <div class="flex">
            <button
                @click="tab = 'human'"
                :class="tab === 'human' ? 'border-b-2 border-blue-500 text-dark-text' : 'text-dark-muted hover:text-dark-text'"
                class="px-6 py-3 font-medium text-sm transition"
            >
                👤 Human View
            </button>
            <button
                @click="tab = 'agent'"
                :class="tab === 'agent' ? 'border-b-2 border-blue-500 text-dark-text' : 'text-dark-muted hover:text-dark-text'"
                class="px-6 py-3 font-medium text-sm transition"
            >
                🤖 Agent View
            </button>
        </div>
    </div>

    <!-- Human View Tab -->
    <div x-show="tab === 'human'" class="p-6">
        <h2 class="text-lg font-semibold mb-4">Summary</h2>
        <div class="prose prose-invert max-w-none">
            <p class="text-lg">{{ task.human_summary or task.description[:300] }}</p>
        </div>

        {% if task.phases %}
        <h3 class="text-md font-semibold mt-6 mb-3">Progress</h3>
        <div class="space-y-2">
            {% for phase in task.phases %}
            <div class="flex items-center gap-3 p-3 bg-dark-bg rounded-lg">
                <span class="text-xl">
                    {% if phase.status == 'completed' %}✅
                    {% elif phase.status == 'in_progress' %}🔄
                    {% elif phase.status == 'blocked' %}🚫
                    {% else %}⚪{% endif %}
                </span>
                <div class="flex-1">
                    <div class="font-medium">{{ phase.title }}</div>
                </div>
                <span class="text-xs text-dark-muted capitalize">{{ phase.status.replace('_', ' ') }}</span>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>

    <!-- Agent View Tab -->
    <div x-show="tab === 'agent'" class="p-6">
        <h2 class="text-lg font-semibold mb-4">Full Description</h2>
        <div class="prose prose-invert max-w-none prose-headings:text-dark-text prose-p:text-dark-text prose-li:text-dark-text prose-code:text-blue-300 prose-pre:bg-dark-bg">
            <div
                x-data="{ content: `{{ (task.description or '') | replace('\\', '\\\\') | replace('`', '\\`') | replace('${', '\\${') }}` }"
                x-markdown="content"
            ></div>
        </div>
    </div>
</div>
```

### 8. Update Task List Filter

Add WAITING_FOR_HUMAN to filter in `src/agentjobs/api/templates/task_list.html`:

```html
<select x-model="statusFilter" class="bg-dark-bg border border-dark-border rounded-lg px-4 py-2">
    <option value="all">All Status</option>
    <option value="waiting_for_human">⚠️ Waiting for Human</option>  <!-- NEW, at top -->
    <option value="planned">Planned</option>
    <option value="in_progress">In Progress</option>
    <option value="blocked">Blocked</option>
    <option value="under_review">Under Review</option>
    <option value="completed">Completed</option>
    <option value="archived">Archived</option>
</select>
```

Update status badge component `src/agentjobs/api/templates/components/status_badge.html`:

```html
<span class="px-2 py-1 text-xs rounded
    {% if task.status == 'completed' %}bg-green-900 text-green-200
    {% elif task.status == 'in_progress' %}bg-blue-900 text-blue-200
    {% elif task.status == 'waiting_for_human' %}bg-orange-900 text-orange-200  <!-- NEW -->
    {% elif task.status == 'blocked' %}bg-red-900 text-red-200
    {% elif task.status == 'under_review' %}bg-purple-900 text-purple-200
    {% elif task.status == 'archived' %}bg-gray-800 text-gray-400
    {% else %}bg-gray-700 text-gray-300{% endif %}">
    {{ task.status.replace('_', ' ').title() }}
</span>
```

---

## 9. Create Test Data

Create realistic test tasks in `src/agentjobs/test_data/sample_tasks.py`:

```python
"""Sample test tasks for AgentJobs demo and testing."""

from datetime import datetime, timezone, timedelta
from agentjobs.models import (
    Task,
    TaskStatus,
    Priority,
    Phase,
    Prompts,
    Prompt,
    StatusUpdate,
    Deliverable,
)


def create_sample_tasks() -> list[Task]:
    """Generate sample tasks demonstrating all workflow states."""
    now = datetime.now(tz=timezone.utc)
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    return [
        # Task 1: WAITING_FOR_HUMAN - Database schema design needs approval
        Task(
            id="task-001",
            title="Design Database Schema for User Authentication",
            human_summary="Review and approve the proposed PostgreSQL schema for user authentication including OAuth2 providers.",
            description="""## Context
We need to implement a robust user authentication system supporting multiple OAuth2 providers (Google, GitHub, Microsoft).

## Proposed Schema

### users table
- id (uuid, primary key)
- email (varchar, unique)
- display_name (varchar)
- avatar_url (varchar, nullable)
- created_at (timestamp)
- updated_at (timestamp)

### oauth_accounts table
- id (uuid, primary key)
- user_id (uuid, foreign key to users)
- provider (varchar: 'google', 'github', 'microsoft')
- provider_user_id (varchar)
- access_token (text, encrypted)
- refresh_token (text, encrypted, nullable)
- expires_at (timestamp, nullable)
- created_at (timestamp)
- updated_at (timestamp)

### sessions table
- id (uuid, primary key)
- user_id (uuid, foreign key to users)
- token (varchar, unique, indexed)
- expires_at (timestamp)
- created_at (timestamp)

## Security Considerations
- All tokens encrypted at rest using AES-256
- Session tokens rotated every 24 hours
- Refresh tokens support optional (depends on provider)

## Questions for Human Review
1. Should we support multiple OAuth accounts per user (same provider)?
2. Do we need audit logging for authentication events?
3. What's the session timeout policy? (currently 24h)
""",
            status=TaskStatus.WAITING_FOR_HUMAN,
            priority=Priority.HIGH,
            category="architecture",
            assigned_to="Codex",
            estimated_effort="2-3 days",
            created=two_days_ago,
            updated=yesterday,
            tags=["security", "database", "authentication"],
            phases=[
                Phase(
                    id="phase-1",
                    title="Schema Design",
                    status=TaskStatus.COMPLETED,
                    notes="Completed initial schema design based on OAuth2 best practices.",
                    completed_at=yesterday,
                ),
                Phase(
                    id="phase-2",
                    title="Human Review & Approval",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    notes="Schema ready for review. Awaiting feedback on multi-account support and audit logging requirements.",
                ),
                Phase(
                    id="phase-3",
                    title="Implementation",
                    status=TaskStatus.PLANNED,
                    notes="Create migration scripts and ORM models after approval.",
                ),
            ],
            prompts=Prompts(
                starter="Design a PostgreSQL schema for user authentication supporting OAuth2 providers (Google, GitHub, Microsoft).",
                followups=[
                    Prompt(
                        timestamp=yesterday,
                        author="Codex",
                        content="Schema design complete. Added encryption for tokens and session management. Ready for human review.",
                    ),
                ],
            ),
            status_updates=[
                StatusUpdate(
                    timestamp=yesterday,
                    author="Codex",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    summary="Schema design complete, awaiting approval",
                    details="Designed 3-table schema with security best practices. Need human input on multi-account support and audit logging.",
                ),
            ],
            deliverables=[
                Deliverable(
                    path="docs/schema/auth_schema.sql",
                    status="completed",
                    description="PostgreSQL schema DDL",
                ),
                Deliverable(
                    path="docs/schema/auth_erd.png",
                    status="completed",
                    description="Entity relationship diagram",
                ),
            ],
        ),

        # Task 2: WAITING_FOR_HUMAN - API endpoint behavior needs clarification
        Task(
            id="task-002",
            title="Implement Rate Limiting for Public API",
            human_summary="Approve rate limiting strategy: 100 req/min for free tier, 1000 req/min for paid. Need decision on penalty box duration.",
            description="""## Objective
Protect our public API from abuse while ensuring good UX for legitimate users.

## Proposed Strategy

### Rate Limits by Tier
- **Free tier**: 100 requests/minute, 10,000 requests/day
- **Paid tier**: 1,000 requests/minute, 100,000 requests/day
- **Enterprise**: Custom limits (negotiated per contract)

### Implementation
- Use Redis for distributed rate limiting (sliding window)
- Return HTTP 429 with `Retry-After` header when exceeded
- Include rate limit info in response headers:
  - `X-RateLimit-Limit`: Maximum requests allowed
  - `X-RateLimit-Remaining`: Requests remaining in window
  - `X-RateLimit-Reset`: Unix timestamp when limit resets

### Penalty Box (NEEDS DECISION)
When users repeatedly exceed limits:
- **Option A**: 15-minute cooldown (recommended for good UX)
- **Option B**: 1-hour escalating penalty (stronger deterrent)
- **Option C**: No penalty box, just standard limiting

### Edge Cases
- Burst allowance: Allow 20% burst above limit for 5 seconds
- Whitelisted IPs (internal services, monitoring): No limits
- Rate limit bypass for emergencies: Admin override flag

## Questions
1. Which penalty box strategy? (A, B, or C)
2. Should we log rate limit violations for security monitoring?
3. Do we need per-endpoint limits (e.g., auth endpoints more strict)?
""",
            status=TaskStatus.WAITING_FOR_HUMAN,
            priority=Priority.CRITICAL,
            category="infrastructure",
            assigned_to="Codex",
            estimated_effort="1 week",
            created=two_days_ago,
            updated=now - timedelta(hours=4),
            tags=["api", "security", "performance"],
            phases=[
                Phase(
                    id="phase-1",
                    title="Research & Design",
                    status=TaskStatus.COMPLETED,
                    notes="Researched industry standards (Stripe, GitHub, Twilio). Designed sliding window algorithm with Redis.",
                    completed_at=yesterday,
                ),
                Phase(
                    id="phase-2",
                    title="Policy Approval",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    notes="Need human decision on penalty box strategy and per-endpoint limits.",
                ),
            ],
            prompts=Prompts(starter="Design and implement rate limiting for our public API to prevent abuse."),
            status_updates=[
                StatusUpdate(
                    timestamp=now - timedelta(hours=4),
                    author="Codex",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    summary="Rate limiting design ready, need policy decisions",
                    details="Technical design is complete. Need human input on penalty box duration and per-endpoint limit strategy.",
                ),
            ],
        ),

        # Task 3: IN_PROGRESS - Active development
        Task(
            id="task-003",
            title="Add Dark Mode Toggle to Settings Page",
            human_summary="Adding dark mode support with system preference detection and manual toggle in settings.",
            description="""## Implementation Plan
1. Add theme context provider to React app
2. Create toggle component in settings
3. Store preference in localStorage
4. Detect system preference on first load
5. Apply CSS custom properties for color theming
""",
            status=TaskStatus.IN_PROGRESS,
            priority=Priority.MEDIUM,
            category="feature",
            assigned_to="Codex",
            estimated_effort="3 days",
            created=now - timedelta(days=1),
            updated=now - timedelta(hours=2),
            tags=["ui", "accessibility"],
            phases=[
                Phase(id="phase-1", title="Theme Context", status=TaskStatus.COMPLETED, completed_at=now - timedelta(hours=6)),
                Phase(id="phase-2", title="Toggle Component", status=TaskStatus.IN_PROGRESS),
                Phase(id="phase-3", title="CSS Variables", status=TaskStatus.PLANNED),
            ],
            prompts=Prompts(starter="Add dark mode toggle to settings with system preference detection."),
        ),

        # Task 4: BLOCKED - Waiting on external dependency
        Task(
            id="task-004",
            title="Migrate to PostgreSQL 16",
            human_summary="PostgreSQL 16 migration blocked - DevOps needs to provision new database instances in production.",
            description="""## Migration Plan
Upgrade from PostgreSQL 14 to 16 for performance improvements and new features.

**Blocked on**: DevOps team provisioning new PostgreSQL 16 instances in production
**Blocker ticket**: DEVOPS-892

## Testing Status
- ✅ Dev environment migrated successfully
- ✅ Staging environment migrated successfully
- ❌ Production instances not yet provisioned
""",
            status=TaskStatus.BLOCKED,
            priority=Priority.HIGH,
            category="infrastructure",
            assigned_to="Codex",
            estimated_effort="1 day (once unblocked)",
            created=now - timedelta(days=5),
            updated=now - timedelta(days=1),
            tags=["database", "infrastructure"],
            prompts=Prompts(starter="Plan and execute migration to PostgreSQL 16."),
        ),

        # Task 5: WAITING_FOR_HUMAN - Cost approval needed
        Task(
            id="task-005",
            title="Enable CloudWatch Advanced Monitoring",
            human_summary="Request approval for $150/month CloudWatch advanced monitoring. Provides 1-second granularity metrics for RDS.",
            description="""## Proposal
Enable CloudWatch Enhanced Monitoring for RDS instances to get 1-second granularity metrics.

## Cost
- Current: $0/month (basic monitoring only)
- Proposed: ~$150/month ($2.50 per instance × 60 instances)

## Benefits
- 1-second metric granularity (vs 1-minute)
- OS-level metrics (CPU, memory, disk I/O)
- Better troubleshooting during incidents
- Proactive performance optimization

## Business Case
Last month we had 3 database incidents that took >2 hours to diagnose due to lack of granular metrics.
Expected time savings: ~6 hours/month × $200/hour = $1,200/month value.

ROI: 8x return on investment.

**Awaiting approval from Engineering Manager or CTO.**
""",
            status=TaskStatus.WAITING_FOR_HUMAN,
            priority=Priority.LOW,
            category="infrastructure",
            assigned_to="Codex",
            estimated_effort="1 hour to enable",
            created=now - timedelta(days=3),
            updated=now - timedelta(days=2),
            tags=["monitoring", "cost", "rds"],
            prompts=Prompts(starter="Research and propose CloudWatch enhanced monitoring for RDS instances."),
        ),

        # Task 6: COMPLETED - Recently finished
        Task(
            id="task-006",
            title="Fix Memory Leak in WebSocket Handler",
            human_summary="Fixed memory leak caused by unclosed event listeners in WebSocket connections.",
            description="""## Problem
WebSocket handler was leaking ~50MB/hour due to event listeners not being cleaned up on disconnect.

## Solution
- Added cleanup handler for connection close events
- Implemented WeakMap for connection tracking
- Added memory usage monitoring

## Results
- Memory usage stable at ~200MB (was growing to 2GB+ daily)
- No more daily restarts required
- Performance improved by 15%
""",
            status=TaskStatus.COMPLETED,
            priority=Priority.CRITICAL,
            category="bugfix",
            assigned_to="Codex",
            estimated_effort="2 days",
            created=now - timedelta(days=4),
            updated=now - timedelta(hours=8),
            tags=["performance", "websocket", "memory"],
            phases=[
                Phase(id="phase-1", title="Investigation", status=TaskStatus.COMPLETED, completed_at=now - timedelta(days=2)),
                Phase(id="phase-2", title="Fix Implementation", status=TaskStatus.COMPLETED, completed_at=now - timedelta(days=1)),
                Phase(id="phase-3", title="Testing & Monitoring", status=TaskStatus.COMPLETED, completed_at=now - timedelta(hours=8)),
            ],
            prompts=Prompts(starter="Debug and fix memory leak in WebSocket handler."),
            deliverables=[
                Deliverable(path="src/websocket/handler.ts", status="completed", description="Fixed WebSocket handler"),
                Deliverable(path="tests/websocket/memory_test.ts", status="completed", description="Memory leak regression test"),
            ],
        ),

        # Task 7: PLANNED - Future work
        Task(
            id="task-007",
            title="Implement GraphQL Pagination with Cursor Strategy",
            human_summary="Plan and implement cursor-based pagination for GraphQL API to replace offset-based approach.",
            description="""## Objective
Replace offset-based pagination with cursor-based for better performance and consistency.

## Design
- Use base64-encoded cursors containing (timestamp, id)
- Implement forward/backward pagination
- Add `pageInfo` with hasNextPage/hasPreviousPage
- Support first/last/before/after arguments

## Rollout
1. Implement new cursor fields alongside existing offset
2. Deprecate offset pagination (6-month notice)
3. Remove offset fields in v2.0 API
""",
            status=TaskStatus.PLANNED,
            priority=Priority.MEDIUM,
            category="feature",
            assigned_to=None,
            estimated_effort="1 week",
            created=now - timedelta(hours=12),
            updated=now - timedelta(hours=12),
            tags=["graphql", "api", "performance"],
            prompts=Prompts(starter="Design and implement cursor-based pagination for GraphQL API."),
        ),
    ]
```

Create CLI command to load test data in `src/agentjobs/cli.py`:

```python
@app.command()
def load_test_data(
    storage_dir: str = typer.Option("./tasks", help="Directory for task storage"),
):
    """Load sample test data for demo and testing."""
    from agentjobs.storage import TaskStorage
    from agentjobs.manager import TaskManager
    from agentjobs.test_data.sample_tasks import create_sample_tasks

    storage = TaskStorage(Path(storage_dir))
    manager = TaskManager(storage)

    tasks = create_sample_tasks()
    for task in tasks:
        try:
            manager.create_task(**task.model_dump())
            typer.echo(f"✓ Created {task.id}: {task.title}")
        except ValueError:
            # Task already exists, update it
            manager.replace_task(task.id, **task.model_dump())
            typer.echo(f"↻ Updated {task.id}: {task.title}")

    typer.echo(f"\n✅ Loaded {len(tasks)} test tasks")
    typer.echo(f"   - {sum(1 for t in tasks if t.status == TaskStatus.WAITING_FOR_HUMAN)} waiting for human")
    typer.echo(f"   - {sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)} in progress")
    typer.echo(f"   - {sum(1 for t in tasks if t.status == TaskStatus.BLOCKED)} blocked")
    typer.echo(f"   - {sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)} completed")
    typer.echo(f"   - {sum(1 for t in tasks if t.status == TaskStatus.PLANNED)} planned")
```

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Run unit tests to ensure model changes work
PYTHONPATH=src .venv/bin/pytest tests/ -v

# Load test data showcasing all workflow states
.venv/bin/agentjobs load-test-data --storage-dir ./test_tasks

# Start server pointing to test data
.venv/bin/agentjobs serve --storage-dir ./test_tasks

# Test in browser at http://localhost:8765
# Check:
# ✓ Dashboard shows "Waiting for Human" spotlight with 3 tasks
# ✓ Nav bar has red notification badge showing "3"
# ✓ Task detail has Human View / Agent View tabs
# ✓ Human View shows concise summary (not verbose implementation)
# ✓ Agent View shows full markdown details
# ✓ Task 001 (Database Schema) shows review questions clearly
# ✓ Task 002 (Rate Limiting) shows policy decision needed
# ✓ Task list filter includes "Waiting for Human" option
# ✓ Status badges show orange for waiting_for_human
# ✓ All 7 test tasks display correctly with different statuses
```

---

## Acceptance Criteria

**Data Model:**
- [ ] WAITING_FOR_HUMAN status added to TaskStatus enum
- [ ] Task model has human_summary field
- [ ] Migration parser extracts concise human summaries (1-2 sentences)

**Dashboard UI:**
- [ ] Dashboard shows spotlight section for waiting tasks (top of page)
- [ ] Spotlight shows up to 3 waiting tasks with summaries
- [ ] Nav bar shows red notification badge with count (e.g., "3")
- [ ] Dashboard stats include "Waiting for Human" card

**Task Detail UI:**
- [ ] Task detail has Human View / Agent View tabbed interface
- [ ] Human View shows concise summary + progress (no implementation details)
- [ ] Agent View shows full markdown description
- [ ] Tabs switch smoothly with Alpine.js

**Task List UI:**
- [ ] Task list filter includes "Waiting for Human" option at top
- [ ] Status badge shows orange color for waiting_for_human
- [ ] Filter correctly shows waiting tasks

**Test Data:**
- [ ] 7 test tasks created covering all workflow states
- [ ] 3 tasks in WAITING_FOR_HUMAN status (database schema, rate limiting, cost approval)
- [ ] Test tasks have realistic human summaries and agent descriptions
- [ ] CLI command `load-test-data` works correctly
- [ ] Test tasks include phases, prompts, status updates, deliverables

**Quality:**
- [ ] All unit tests pass
- [ ] No console errors in browser
- [ ] Dark theme preserved throughout
- [ ] Markdown renders correctly in all views

---

## Commit

```bash
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(ux): add waiting-for-human workflow and human/agent views

Separate human context from agent implementation details for better UX.

Changes:
- Add WAITING_FOR_HUMAN status to workflow
- Add human_summary field to Task model
- Dashboard spotlight section for tasks needing human attention
- Nav bar notification badge showing count of waiting tasks
- Task detail tabbed view (Human View vs Agent View)
- Human View: concise summary + progress overview
- Agent View: full markdown implementation details
- Migration parser extracts 1-2 sentence human summaries
- Task list filter includes waiting status
- Orange status badge for waiting_for_human
- Test data: 7 realistic tasks covering all workflow states
- CLI command: load-test-data for demo/testing

Before: Mixed human/agent content, no clear workflow for human input
After: Clear separation, spotlight for tasks needing attention

git push origin main
```

---

## Notes

**Design Rationale:**
- **Human View** = "What do I need to know?" (concise, high-level)
- **Agent View** = "How do I implement this?" (detailed, technical)
- **Notification badge** = Standard UI pattern (like GitHub, Slack)
- **Spotlight section** = Prominent, actionable, limited to 3 items (reduces overwhelm)
- **Orange color** = Warning (needs attention) but not error (red = blocked)

**Future Enhancements (not in this phase):**
- Email/Slack notifications when task status → waiting_for_human
- Comment system for human ↔ agent communication
- Approval workflow (approve/reject with notes)
- Time tracking for human review time

Ready to make this human-friendly! 👤🤖
