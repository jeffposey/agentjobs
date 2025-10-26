# Task 031 - Phase 5.3: Bootstrap AgentJobs Self-Management

---
## ⚠️ CRITICAL: META-CIRCULAR WORK ⚠️

**This is special:** We're using AgentJobs to manage AgentJobs development.

**Working directory:** `/mnt/c/projects/agentjobs`

---

## Context

**Current situation:**
- Task 031 (building AgentJobs) lives in privateproject repo
- 1 task file: `tasks/agentjobs/task-031-tasks-2-0-system.yaml`
- 9 phase prompts: `prompts/task-031-phase-*.md`
- All about building agentjobs, not privateproject

**Problem:** Using privateproject's task system to manage agentjobs development. Should be self-managed!

**Goal:** Move task 031 into agentjobs repo and use AgentJobs to manage itself going forward.

---

## Objectives

1. Create `prompts/` directory in agentjobs repo
2. Copy task 031 prompts from privateproject to agentjobs
3. Migrate task 031 to agentjobs `tasks/agentjobs/` using migration tool
4. Create task 032 (future agentjobs work) using agentjobs CLI
5. Update agentjobs README to reference self-managed tasks
6. Test full workflow (agents using agentjobs to work on agentjobs)

---

## Implementation

### 1. Create Prompts Directory in AgentJobs

```bash
cd /mnt/c/projects/agentjobs

# Create prompts directory (for agent instructions)
mkdir -p prompts

# Create README
cat > prompts/README.md << 'EOF'
# AgentJobs Prompts

Agent instructions for AgentJobs development tasks.

## Structure

- `task-XXX-phase-Y.md` - Phase-specific prompts
- Each prompt contains detailed implementation instructions
- Prompts are referenced in task YAML files

## Usage

Prompts are automatically linked during migration:

\`\`\`bash
agentjobs migrate \
  'tasks/agentjobs/task-*.yaml' \
  tasks/agentjobs/ \
  --prompts-dir prompts
\`\`\`

Or manually referenced in YAML:

\`\`\`yaml
prompts:
  starter: prompts/task-032-phase-1.md
  followups:
    - prompt_file: prompts/task-032-phase-2.md
\`\`\`
EOF
```

---

### 2. Copy Task 031 Files from PrivateProject

```bash
cd /mnt/c/projects/agentjobs

# Copy prompts
cp /mnt/c/projects/privateproject/ops/prompts/task-031-*.md prompts/

# Count files
ls prompts/task-031-*.md | wc -l
# Should show: 9 files

# List files
ls -lah prompts/task-031-*.md
# Should show all phase prompts
```

---

### 3. Migrate Task 031 to AgentJobs

**Option A: Using Migration Tool (Recommended)**

Create markdown file first, then migrate:

```bash
cd /mnt/c/projects/agentjobs

# Copy task markdown from privateproject
cp /mnt/c/projects/privateproject/ops/tasks/task-031-tasks-2.0-system.md /tmp/task-031.md

# Migrate it
agentjobs migrate \
  '/tmp/task-031.md' \
  tasks/agentjobs/ \
  --prompts-dir prompts

# Verify
ls -lah tasks/agentjobs/task-031-*
cat tasks/agentjobs/task-031-*.yaml | head -40
```

**Option B: Create Manually (Alternative)**

If migration doesn't work well:

```bash
cd /mnt/c/projects/agentjobs

# Use CLI to create task
agentjobs create \
  --title "Tasks 2.0 - Agent Task Management System" \
  --priority high \
  --category infrastructure \
  --assigned-to "Claude + Codex" \
  --estimated-effort "2-3 weeks"

# This creates task-001.yaml (or next available ID)
# Manually edit to add:
# - Phases (1-5.3)
# - Prompts references
# - Status updates from completed work
```

---

### 4. Update Task 031 Status

Edit the migrated `tasks/agentjobs/task-031-*.yaml` to reflect current progress:

```yaml
# Update status
status: in_progress  # We're on phase 5.3!

# Update phases
phases:
  - id: phase-1
    title: Core Infrastructure
    status: completed
    completed_at: 2025-10-25T20:00:00Z
    notes: YAML storage, models, manager complete

  - id: phase-2
    title: REST API
    status: completed
    completed_at: 2025-10-25T21:00:00Z
    notes: FastAPI with full CRUD operations

  - id: phase-3
    title: Web GUI
    status: completed
    completed_at: 2025-10-25T22:00:00Z
    notes: Dark theme browser interface

  - id: phase-4
    title: Migration Tool
    status: completed
    completed_at: 2025-10-25T23:00:00Z
    notes: Markdown to YAML conversion

  - id: phase-4.1
    title: Improve Task Rendering
    status: completed
    completed_at: 2025-10-26T10:00:00Z
    notes: Marked.js markdown rendering

  - id: phase-4.2
    title: Human-Agent UX
    status: completed
    completed_at: 2025-10-26T17:00:00Z
    notes: Waiting status, tabbed views, test data

  - id: phase-5.1
    title: Package and Integrate
    status: planned
    notes: Install in privateproject, migrate tasks

  - id: phase-5.2
    title: Agent DX Improvements
    status: in_progress
    notes: Examples, docs, CLI command

  - id: phase-5.3
    title: Bootstrap Self-Management
    status: in_progress
    notes: Use AgentJobs to manage itself

# Add prompt references
prompts:
  starter: prompts/task-031-phase-1-core-infrastructure.md
  followups:
    - timestamp: 2025-10-25T20:00:00Z
      author: Claude
      prompt_file: prompts/task-031-phase-2-rest-api.md
      context: Phase 1 complete

    - timestamp: 2025-10-25T21:00:00Z
      author: Claude
      prompt_file: prompts/task-031-phase-3-web-gui.md
      context: Phase 2 complete

    # ... etc for all phases

# Add status updates
status_updates:
  - timestamp: 2025-10-26T17:00:00Z
    author: Claude
    status: in_progress
    summary: Phase 4.2 complete - Human/Agent workflow finished
    details: Added waiting-for-human status, tabbed views, test data

  - timestamp: 2025-10-26T18:00:00Z
    author: Claude
    status: in_progress
    summary: Phase 5.2 started - Agent DX improvements
    details: Adding examples and documentation
```

---

### 5. Create Task 032 (Future Work)

Create the next agentjobs development task using the CLI:

```bash
cd /mnt/c/projects/agentjobs

agentjobs create \
  --title "AgentJobs v0.2.0 - Production Hardening" \
  --priority medium \
  --category infrastructure \
  --assigned-to "TBD"

# Manually edit tasks/agentjobs/task-032-*.yaml to add:
# - Description of future improvements
# - Phases (auth, deployment, monitoring, etc.)
# - Mark as status: planned
```

Example task 032 content:

```yaml
id: task-032-agentjobs-v0-2-production-hardening
title: "AgentJobs v0.2.0 - Production Hardening"
status: planned
priority: medium
category: infrastructure
assigned_to: TBD
estimated_effort: 2-3 weeks

human_summary: "Prepare AgentJobs for production: auth, deployment automation, monitoring, and GitHub Issues sync."

description: |
  ## Objective

  Harden AgentJobs for production deployments beyond privateproject.

  ## Scope

  1. **Authentication & Authorization**
     - API key auth for REST endpoints
     - Role-based access (admin, agent, viewer)
     - Session management for web GUI

  2. **Deployment Automation**
     - Docker container
     - docker-compose for full stack
     - Kubernetes manifests (optional)

  3. **Monitoring & Observability**
     - Prometheus metrics
     - Health check endpoints
     - Structured logging

  4. **GitHub Integration**
     - Sync tasks with GitHub Issues
     - Bi-directional updates
     - Webhook support

  5. **Performance & Scaling**
     - Database connection pooling
     - Caching layer (Redis)
     - Load testing

  ## Non-Goals

  - Multi-tenant support (future)
  - GUI task editing (Phase 6)
  - Mobile app (future)

phases:
  - id: phase-1
    title: Authentication System
    status: planned

  - id: phase-2
    title: Docker & Deployment
    status: planned

  - id: phase-3
    title: Monitoring & Logging
    status: planned

  - id: phase-4
    title: GitHub Integration
    status: planned

  - id: phase-5
    title: Performance Optimization
    status: planned

prompts:
  starter: |
    Design and implement production-ready features for AgentJobs v0.2.0.
    See description for full scope and phases.
```

---

### 6. Update AgentJobs README
### 7. Record Historical Phase Tasks

Task 031 now serves as an archived summary. Verify that each historical phase has its own task file tied to the commit that delivered it.

```bash
ls tasks/agentjobs/task-0{33..40}-*.yaml
agentjobs show task-033-phase-1-core-infrastructure
agentjobs show task-040-phase-5-3-self-management
```

Ensure each task includes:
- `external_links` pointing at the matching commit (`af42e6c`, `e6dbfbe`, `b05472b`, `08cab28`, `d4378cd`, `62665e5`, `90f2ac5`)
- `branches` metadata marking `main` as merged with the correct timestamp
- Dependencies linking to the previous phase tasks where applicable

With the phase tasks confirmed, Task 031 can remain archived as the high-level audit record.


Add section about self-management to `README.md`:

```markdown
## Development

AgentJobs uses itself to manage its own development tasks!

### View Development Tasks

\`\`\`bash
# Start server
agentjobs serve

# Open http://localhost:8765
# See tasks for AgentJobs development
\`\`\`

### Contributing

1. Check existing tasks: \`agentjobs list\`
2. Pick a planned task: \`agentjobs show task-XXX\`
3. Start work: \`agentjobs work --agent your-name\`
4. Follow the prompt instructions
5. Submit PR when complete

### Task Structure

- \`tasks/agentjobs/\` - Active AgentJobs roadmap (task-031, task-032, task-033…039)
- \`tasks/test-data/\` - Sample/demo tasks for UI smoke tests
- \`tasks/privateproject/\` - Legacy PrivateProject tasks retained for migration tooling
- \`prompts/\` - Detailed implementation instructions
- \`examples/\` - Agent integration examples
- \`docs/\` - Documentation

**Meta note:** We use AgentJobs to build AgentJobs. Dogfooding at its finest! 🐕
```

---

### 8. Update PrivateProject Task 031

After migrating to agentjobs, update privateproject's task 031:

```bash
cd /mnt/c/projects/privateproject

# Edit ops/tasks/task-031-tasks-2.0-system.md
# Add note at top:
```

```markdown
# Task 031: Tasks 2.0 - Agent Task Management System

**⚠️ MIGRATED:** This task now lives in the agentjobs repo.

**See:** https://github.com/jeffposey/agentjobs/blob/main/tasks/agentjobs/task-031-tasks-2-0-system.yaml

This file kept for historical reference only.

---

[rest of content...]
```

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Verify prompts copied
ls prompts/task-031-*.md
# Should show 9 files

# Verify task migrated
ls tasks/agentjobs/task-031-*.yaml
# Should show 1 file

# Test server with self-managed tasks
agentjobs serve &
sleep 2

# Open browser to http://localhost:8765
# Should see:
# ✓ Task 031 in task list
# ✓ Phases 1-5.3 with status
# ✓ Prompts display correctly
# ✓ Human/Agent views work

# Test CLI
agentjobs show task-031
# Should display full task info

# Test Python client
python << 'EOF'
from agentjobs import TaskClient
client = TaskClient()
task = client.get_task("task-031-tasks-2-0-system")
print(f"Title: {task.title}")
print(f"Status: {task.status}")
print(f"Phases: {len(task.phases)}")
assert len(task.phases) >= 9, "Should have all phases"
print("✅ Task 031 loaded successfully")
EOF

# Kill server
pkill -f "agentjobs serve"
```

---

## Acceptance Criteria

**AgentJobs Repo:**
- [ ] prompts/ directory created
- [ ] 9 task 031 prompts copied from privateproject
- [ ] Task 031 migrated to tasks/
- [ ] Task 031 phases updated to reflect current progress
- [ ] Task 032 created for future work
- [ ] README documents self-management
- [ ] All tests pass

**PrivateProject Repo:**
- [ ] Task 031 markdown marked as migrated
- [ ] Reference added to agentjobs repo

**Testing:**
- [ ] Server shows task 031 correctly
- [ ] Phases display with accurate status
- [ ] Prompts accessible
- [ ] CLI commands work
- [ ] Python client can load task

**Workflow Validation:**
- [ ] Can view agentjobs tasks in agentjobs GUI
- [ ] Can query agentjobs tasks via Python client
- [ ] Can start work on agentjobs task using `agentjobs work`
- [ ] Prompts guide implementation correctly

---

## Commit

**AgentJobs repo:**

```bash
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(meta): bootstrap self-management with task 031

Use AgentJobs to manage AgentJobs development.

Changes:
- Add prompts/ directory for agent instructions
- Copy task 031 prompts from privateproject (9 files)
- Migrate task 031 to tasks/ directory
- Update phases to reflect progress (1-5.3)
- Create task 032 for future work (v0.2.0)
- Update README with development workflow
- Document self-management approach

Before: Task 031 lived in privateproject
After: AgentJobs manages its own development

Meta-circular dogfooding! 🐕"

git push origin main
```

**PrivateProject repo:**

```bash
cd /mnt/c/projects/privateproject

# Edit task 031 to add migration notice
git add ops/tasks/task-031-tasks-2.0-system.md
git commit -m "docs(tasks): mark task 031 as migrated to agentjobs

Task 031 now lives in agentjobs repo.
Kept for historical reference."

git push origin main
```

---

## Benefits

**Validation:**
- Proves AgentJobs works for real projects
- Tests all features in production use
- Surfaces UX issues early

**Transparency:**
- Development visible in agentjobs GUI
- Agents can see their own task queue
- Humans can track progress

**Efficiency:**
- Use own tools to build tools
- API client tested by our own workflows
- Examples based on real usage

**Confidence:**
- "We use it ourselves"
- Shows maturity of system
- Demonstrates trust in product

---

## Future: Full AgentJobs Development in AgentJobs

After phase 5.3, ALL new agentjobs work goes through AgentJobs:

1. **Plan feature** → Create task via CLI or GUI
2. **Claude designs** → Add prompt to prompts/
3. **Codex implements** → Uses `agentjobs work` to get task
4. **Track progress** → Updates via Python client
5. **Complete** → Mark done, visible in dashboard

No more markdown task files. Pure AgentJobs workflow.

---

## Notes

**Why this matters:**
- Validates the product works
- Dogfooding catches issues
- Shows confidence to users
- Self-documenting development

**Migration decision:**
- Only task 031 initially (focused on agentjobs)
- PrivateProject keeps its own tasks
- Clean separation of concerns

Ready to bootstrap! 🚀🔁
