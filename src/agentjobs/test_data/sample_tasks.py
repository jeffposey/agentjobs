"""Sample task fixtures exercising the AgentJobs workflow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from agentjobs.models import (
    Deliverable,
    Phase,
    Priority,
    Prompt,
    Prompts,
    StatusUpdate,
    Task,
    TaskStatus,
)


def create_sample_tasks() -> List[Task]:
    """Generate sample tasks demonstrating all workflow states."""
    now = datetime.now(tz=timezone.utc)
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    return [
        Task(
            id="task-001",
            title="Design Database Schema for User Authentication",
            human_summary=(
                "Review and approve the proposed PostgreSQL schema for user authentication, "
                "including multi-provider OAuth2 support and security policies."
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
                    )
                ],
            ),
            status_updates=[
                StatusUpdate(
                    timestamp=yesterday,
                    author="Codex",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    summary="Schema design complete, awaiting approval",
                    details=(
                        "Designed 3-table schema with security best practices. Need human input on "
                        "multi-account support and audit logging."
                    ),
                )
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
        Task(
            id="task-002",
            title="Implement Rate Limiting for Public API",
            human_summary=(
                "Approve rate limiting strategy: 100 req/min for free tier, 1000 req/min for paid tier. "
                "Decide penalty box duration to unblock engineering."
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
                    notes="Need human decision on penalty box strategy and per-endpoint limit strategy.",
                ),
            ],
            prompts=Prompts(
                starter="Design and implement rate limiting for our public API to prevent abuse."
            ),
            status_updates=[
                StatusUpdate(
                    timestamp=now - timedelta(hours=4),
                    author="Codex",
                    status=TaskStatus.WAITING_FOR_HUMAN,
                    summary="Rate limiting design ready, need policy decisions",
                    details=(
                        "Technical design is complete. Need human input on penalty box duration "
                        "and per-endpoint limit strategy."
                    ),
                )
            ],
        ),
        Task(
            id="task-003",
            title="Add Dark Mode Toggle to Settings Page",
            human_summary=(
                "Adding dark mode support with system preference detection and a manual toggle in settings."
            ),
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
                Phase(
                    id="phase-1",
                    title="Theme Context",
                    status=TaskStatus.COMPLETED,
                    completed_at=now - timedelta(hours=6),
                ),
                Phase(
                    id="phase-2",
                    title="Toggle Component",
                    status=TaskStatus.IN_PROGRESS,
                ),
                Phase(
                    id="phase-3",
                    title="CSS Variables",
                    status=TaskStatus.PLANNED,
                ),
            ],
            prompts=Prompts(
                starter="Add dark mode toggle to settings with system preference detection."
            ),
        ),
        Task(
            id="task-004",
            title="Migrate to PostgreSQL 16",
            human_summary=(
                "PostgreSQL 16 migration blocked until DevOps provisions new production instances."
            ),
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
        Task(
            id="task-005",
            title="Enable CloudWatch Advanced Monitoring",
            human_summary=(
                "Request approval for $150/month CloudWatch advanced monitoring to unlock 1-second RDS metrics."
            ),
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
            prompts=Prompts(
                starter="Research and propose CloudWatch enhanced monitoring for RDS instances."
            ),
        ),
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
                Phase(
                    id="phase-1",
                    title="Investigation",
                    status=TaskStatus.COMPLETED,
                    completed_at=now - timedelta(days=2),
                ),
                Phase(
                    id="phase-2",
                    title="Fix Implementation",
                    status=TaskStatus.COMPLETED,
                    completed_at=now - timedelta(days=1),
                ),
                Phase(
                    id="phase-3",
                    title="Testing & Monitoring",
                    status=TaskStatus.COMPLETED,
                    completed_at=now - timedelta(hours=8),
                ),
            ],
            prompts=Prompts(starter="Debug and fix memory leak in WebSocket handler."),
            deliverables=[
                Deliverable(
                    path="src/websocket/handler.ts",
                    status="completed",
                    description="Fixed WebSocket handler",
                ),
                Deliverable(
                    path="tests/websocket/memory_test.ts",
                    status="completed",
                    description="Memory leak regression test",
                ),
            ],
        ),
        Task(
            id="task-007",
            title="Implement GraphQL Pagination with Cursor Strategy",
            human_summary=(
                "Plan and implement cursor-based pagination for the GraphQL API to replace offset pagination."
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
            status=TaskStatus.PLANNED,
            priority=Priority.MEDIUM,
            category="feature",
            assigned_to=None,
            estimated_effort="1 week",
            created=now - timedelta(hours=12),
            updated=now - timedelta(hours=12),
            tags=["graphql", "api", "performance"],
            prompts=Prompts(
                starter="Design and implement cursor-based pagination for GraphQL API."
            ),
        ),
    ]
