"""Sample task fixtures exercising the AgentJobs workflow (schema v2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from agentjobs.models_v2 import (
    AcceptanceCriterion,
    Assignment,
    Ball,
    BallReason,
    Deliverable,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)


def create_sample_tasks() -> List[Task]:
    """Generate sample tasks demonstrating the v2 state axes and log.

    Every open task carries a ``queue_position``: consistency rule 6 requires one, and
    the design is explicit that no path creates an open task without one -- import,
    migration and this loader included (design doc section 5.1). The numbers are the
    bottom-of-band sequence each band would have got anyway; task-006 is closed and so
    has none.
    """
    now = datetime.now(tz=timezone.utc)
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    return [
        Task(
            id="task-001",
            title="Design Database Schema for User Authentication",
            created=two_days_ago,
            updated=yesterday,
            lifecycle=Lifecycle.ACTIVE,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt=(
                "Review the proposed authentication schema and answer: (1) multiple "
                "OAuth accounts per user for the same provider? (2) audit logging for "
                "authentication events? (3) session timeout policy (currently 24h)?"
            ),
            priority=Priority.HIGH,
            queue_position=100,
            category="architecture",
            effort="2-3 days",
            tags=["security", "database", "authentication"],
            assignment=Assignment(owner="codex"),
            spec=Spec(
                summary=(
                    "Review and approve the proposed PostgreSQL schema for user "
                    "authentication, including multi-provider OAuth2 support and "
                    "security policies."
                ),
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
""",
            ),
            acceptance=[
                AcceptanceCriterion(
                    id="ac-1", text="Schema DDL reviewed and approved", status="pending"
                ),
                AcceptanceCriterion(
                    id="ac-2", text="Token encryption strategy documented", status="met"
                ),
            ],
            deliverables=[
                Deliverable(
                    path="docs/schema/auth_schema.sql", status="done", note="PostgreSQL schema DDL"
                ),
                Deliverable(
                    path="docs/schema/auth_erd.png",
                    status="done",
                    note="Entity relationship diagram",
                ),
            ],
            log=[
                LogEntry(
                    id=1,
                    ts=two_days_ago,
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=yesterday,
                    actor="codex",
                    type=LogEntryType.HANDOFF,
                    data={"ball": "human", "ball_reason": "decision"},
                    body=(
                        "Designed 3-table schema with security best practices. Need "
                        "human input on multi-account support and audit logging."
                    ),
                ),
            ],
        ),
        Task(
            id="task-002",
            title="Implement Rate Limiting for Public API",
            created=two_days_ago,
            updated=now - timedelta(hours=4),
            lifecycle=Lifecycle.ACTIVE,
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt=(
                "Pick the penalty box strategy (15-minute cooldown, 1-hour escalating, "
                "or none) and decide whether auth endpoints get stricter per-endpoint "
                "limits."
            ),
            priority=Priority.CRITICAL,
            queue_position=100,
            category="infrastructure",
            effort="1 week",
            tags=["api", "security", "performance"],
            assignment=Assignment(owner="codex"),
            spec=Spec(
                summary=(
                    "Approve rate limiting strategy: 100 req/min for free tier, 1000 "
                    "req/min for paid tier. Decide penalty box duration to unblock "
                    "engineering."
                ),
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
""",
            ),
            log=[
                LogEntry(
                    id=1,
                    ts=two_days_ago,
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=yesterday,
                    actor="codex",
                    type=LogEntryType.PROGRESS,
                    body=(
                        "Researched industry standards (Stripe, GitHub, Twilio). "
                        "Designed sliding window algorithm with Redis."
                    ),
                ),
                LogEntry(
                    id=3,
                    ts=now - timedelta(hours=4),
                    actor="codex",
                    type=LogEntryType.HANDOFF,
                    data={"ball": "human", "ball_reason": "decision"},
                    body=(
                        "Technical design is complete. Need human input on penalty box "
                        "duration and per-endpoint limit strategy."
                    ),
                ),
            ],
        ),
        Task(
            id="task-003",
            title="Add Dark Mode Toggle to Settings Page",
            created=now - timedelta(days=1),
            updated=now - timedelta(hours=2),
            lifecycle=Lifecycle.ACTIVE,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Execute the spec; log progress and hand off when done.",
            priority=Priority.MEDIUM,
            queue_position=100,
            category="feature",
            effort="3 days",
            tags=["ui", "accessibility"],
            assignment=Assignment(owner="codex"),
            spec=Spec(
                summary=(
                    "Adding dark mode support with system preference detection and a "
                    "manual toggle in settings."
                ),
                description="""## Implementation Plan
1. Add theme context provider to React app
2. Create toggle component in settings
3. Store preference in localStorage
4. Detect system preference on first load
5. Apply CSS custom properties for color theming
""",
            ),
            log=[
                LogEntry(
                    id=1,
                    ts=now - timedelta(days=1),
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=now - timedelta(hours=2),
                    actor="codex",
                    type=LogEntryType.PROGRESS,
                    body="Theme context landed; toggle component in progress.",
                ),
            ],
        ),
        Task(
            id="task-004",
            title="Migrate to PostgreSQL 16",
            created=now - timedelta(days=5),
            updated=now - timedelta(days=1),
            lifecycle=Lifecycle.ACTIVE,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.SERVICE,
            ball_prompt=(
                "Waiting on DevOps to provision PostgreSQL 16 production instances "
                "(DEVOPS-892). Resume the cutover once they exist."
            ),
            priority=Priority.HIGH,
            queue_position=200,
            category="infrastructure",
            effort="1 day (once unblocked)",
            tags=["database", "infrastructure"],
            assignment=Assignment(owner="codex"),
            spec=Spec(
                summary=(
                    "PostgreSQL 16 migration blocked until DevOps provisions new "
                    "production instances."
                ),
                description="""## Migration Plan
Upgrade from PostgreSQL 14 to 16 for performance improvements and new features.

**Blocked on**: DevOps team provisioning new PostgreSQL 16 instances in production
**Blocker ticket**: DEVOPS-892

## Testing Status
- Dev environment migrated successfully
- Staging environment migrated successfully
- Production instances not yet provisioned
""",
            ),
            log=[
                LogEntry(
                    id=1,
                    ts=now - timedelta(days=5),
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=now - timedelta(days=1),
                    actor="codex",
                    type=LogEntryType.HANDOFF,
                    data={"ball": "external", "ball_reason": "service"},
                    body="Dev and staging migrated. Production blocked on DEVOPS-892.",
                ),
            ],
        ),
        Task(
            id="task-005",
            title="Enable CloudWatch Advanced Monitoring",
            created=now - timedelta(days=3),
            updated=now - timedelta(days=2),
            lifecycle=Lifecycle.ACTIVE,
            ball=Ball.HUMAN,
            ball_reason=BallReason.APPROVAL,
            ball_prompt=(
                "Approve or decline ~$150/month for CloudWatch Enhanced Monitoring "
                "across 60 RDS instances (estimated 8x ROI)."
            ),
            priority=Priority.LOW,
            queue_position=100,
            category="infrastructure",
            effort="1 hour to enable",
            tags=["monitoring", "cost", "rds"],
            assignment=Assignment(owner="codex"),
            spec=Spec(
                summary=(
                    "Request approval for $150/month CloudWatch advanced monitoring to "
                    "unlock 1-second RDS metrics."
                ),
                description="""## Proposal
Enable CloudWatch Enhanced Monitoring for RDS instances to get 1-second granularity metrics.

## Cost
- Current: $0/month (basic monitoring only)
- Proposed: ~$150/month ($2.50 per instance x 60 instances)

## Benefits
- 1-second metric granularity (vs 1-minute)
- OS-level metrics (CPU, memory, disk I/O)
- Better troubleshooting during incidents
- Proactive performance optimization

## Business Case
Last month we had 3 database incidents that took >2 hours to diagnose due to lack of granular metrics.
Expected time savings: ~6 hours/month x $200/hour = $1,200/month value.

ROI: 8x return on investment.
""",
            ),
            log=[
                LogEntry(
                    id=1,
                    ts=now - timedelta(days=3),
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=now - timedelta(days=2),
                    actor="codex",
                    type=LogEntryType.HANDOFF,
                    data={"ball": "human", "ball_reason": "approval"},
                    body="Proposal complete. Awaiting spend approval from Engineering Manager or CTO.",
                ),
            ],
        ),
        Task(
            id="task-006",
            title="Fix Memory Leak in WebSocket Handler",
            created=now - timedelta(days=4),
            updated=now - timedelta(hours=8),
            lifecycle=Lifecycle.CLOSED,
            outcome=Outcome.COMPLETED,
            priority=Priority.CRITICAL,
            category="bugfix",
            effort="2 days",
            tags=["performance", "websocket", "memory"],
            spec=Spec(
                summary=(
                    "Fixed memory leak caused by unclosed event listeners in WebSocket "
                    "connections."
                ),
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
            ),
            deliverables=[
                Deliverable(
                    path="src/websocket/handler.ts", status="done", note="Fixed WebSocket handler"
                ),
                Deliverable(
                    path="tests/websocket/memory_test.ts",
                    status="done",
                    note="Memory leak regression test",
                ),
            ],
            log=[
                LogEntry(
                    id=1,
                    ts=now - timedelta(days=4),
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "active", "ball": "agent", "ball_reason": "work"},
                    body="Claimed by codex.",
                ),
                LogEntry(
                    id=2,
                    ts=now - timedelta(days=1),
                    actor="codex",
                    type=LogEntryType.PROGRESS,
                    body="Leak isolated to unremoved listeners; fix and WeakMap tracking implemented.",
                ),
                LogEntry(
                    id=3,
                    ts=now - timedelta(hours=8),
                    actor="codex",
                    type=LogEntryType.TRANSITION,
                    data={"lifecycle": "closed", "outcome": "completed"},
                    body="Memory stable at ~200MB over 48h of monitoring. Closed: completed.",
                ),
            ],
        ),
        Task(
            id="task-007",
            title="Implement GraphQL Pagination with Cursor Strategy",
            created=now - timedelta(hours=12),
            updated=now - timedelta(hours=12),
            lifecycle=Lifecycle.DRAFT,
            ball=Ball.HUMAN,
            ball_reason=BallReason.SPEC,
            ball_prompt="Finish specifying this task: confirm the rollout timeline and cursor format.",
            priority=Priority.MEDIUM,
            queue_position=200,
            category="feature",
            effort="1 week",
            tags=["graphql", "api", "performance"],
            spec=Spec(
                summary=(
                    "Plan and implement cursor-based pagination for the GraphQL API to "
                    "replace offset pagination."
                ),
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
            ),
        ),
    ]
