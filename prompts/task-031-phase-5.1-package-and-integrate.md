# Task 031 - Phase 5.1: Package AgentJobs & Integrate with PrivateProject

---
## ⚠️ CRITICAL: MULTI-REPO WORK ⚠️

**You will work in BOTH repositories:**
1. `/mnt/c/projects/agentjobs` - Package and prepare for installation
2. `/mnt/c/projects/privateproject` - Install, configure, and migrate tasks

---

## Context

Phases 1-4.2 complete in standalone agentjobs repo:
- ✅ Core infrastructure (YAML storage, models, manager)
- ✅ REST API (FastAPI with full CRUD)
- ✅ Browser GUI (dark theme, Alpine.js)
- ✅ Migration tool (markdown → YAML)
- ✅ Markdown rendering (marked.js, prose styles)
- ✅ Human/Agent workflow (waiting status, tabbed views, test data)

**Current state:**
- **agentjobs**: 7 sample test tasks, all features working
- **privateproject**: 34 legacy markdown tasks in `ops/tasks/*.md`

**Now:** Package agentjobs and integrate into privateproject as a proper dependency.

---

## Objectives

1. Verify agentjobs package is installable
2. Add agentjobs to privateproject dependencies
3. Install in privateproject venv
4. Test installation with sample data
5. Migrate all 34 privateproject tasks
6. Update engineering docs
7. Clean up and commit

---

## Implementation

### 1. Verify AgentJobs Package (agentjobs repo)

```bash
cd /mnt/c/projects/agentjobs

# Verify package structure
ls -la src/agentjobs/
# Should see: __init__.py, cli.py, models.py, manager.py, storage.py, api/, migration/, test_data/

# Check pyproject.toml has CLI entrypoint
grep -A 2 "\[tool.poetry.scripts\]" pyproject.toml
# Should show: agentjobs = "agentjobs.cli:app"

# Verify tests pass
PYTHONPATH=src .venv/bin/pytest tests/ -v
# Should show: 46 passed

# Check current git status
git status
# Should be clean (all phase 4.2 changes committed)
```

---

### 2. Add AgentJobs to PrivateProject Dependencies

**Edit `/mnt/c/projects/privateproject/pyproject.toml`:**

Add to `[project.dependencies]` array:

```toml
[project]
dependencies = [
    # ... existing dependencies ...
    "agentjobs @ file:///mnt/c/projects/agentjobs",
]
```

**Alternative if above doesn't work:**

Install as editable without adding to pyproject.toml:
```bash
cd /mnt/c/projects/privateproject
.venv/bin/pip install -e /mnt/c/projects/agentjobs
```

---

### 3. Install in PrivateProject (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Install with dependencies
.venv/bin/pip install -e /mnt/c/projects/agentjobs

# Verify installation
.venv/bin/agentjobs --version
# Should show: agentjobs CLI version ...

# Test help
.venv/bin/agentjobs --help
# Should show all commands: init, list, show, create, serve, migrate, load-test-data
```

---

### 4. Test with Sample Data (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Create temporary test directory
mkdir -p test_agentjobs_install

# Load sample test data to verify everything works
cd test_agentjobs_install
../.venv/bin/agentjobs load-test-data
# Should create 7 test tasks (3 waiting for human, 1 in_progress, 1 blocked, 1 completed, 1 planned)

# Start server to verify GUI works
../.venv/bin/agentjobs serve &
sleep 3
# Server should start on http://localhost:8765

# Kill server
pkill -f "agentjobs serve"

# Clean up test data
cd ..
rm -rf test_agentjobs_install

# Verification passed if:
# ✓ 7 tasks created
# ✓ Server started without errors
# ✓ All commands worked
```

---

### 5. Migrate PrivateProject Tasks (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Create tasks directory
mkdir -p tasks

# DRY RUN first to check for issues
.venv/bin/agentjobs migrate \
  'ops/tasks/task-*.md' \
  /tmp/migration-dry-run \
  --prompts-dir ops/prompts \
  --dry-run

# Review output for warnings/errors
# Check: All 34 tasks should be listed

# If dry run looks good, run actual migration
.venv/bin/agentjobs migrate \
  'ops/tasks/task-*.md' \
  tasks/ \
  --prompts-dir ops/prompts

# Verify tasks were created
ls -lah tasks/
# Should show 34 YAML files: task-001.yaml through task-034.yaml (or similar)

# Check a few migrated tasks
.venv/bin/agentjobs show task-016
.venv/bin/agentjobs show task-030
.venv/bin/agentjobs show task-031

# Verify human_summary was extracted
cat tasks/task-016.yaml | grep "human_summary"
# Should show concise summary extracted from markdown
```

---

### 6. Test Migrated Tasks (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Start server (uses ./tasks by default)
.venv/bin/agentjobs serve
# Server runs on http://localhost:8765

# Open browser and verify:
# ✓ Dashboard shows all 34 tasks
# ✓ Task counts are correct (in_progress, completed, etc.)
# ✓ Task detail pages render correctly
# ✓ Human View / Agent View tabs work
# ✓ Markdown renders (headings, bullets, code blocks)
# ✓ Phases show with status
# ✓ Deliverables display
# ✓ Prompts are preserved
# ✓ Search and filters work

# Kill server when done
# Ctrl+C or pkill -f "agentjobs serve"
```

---

### 7. Update Documentation (privateproject repo)

**Update `ENGINEERING.md` - Add section after "Commit Discipline":**

```markdown
## Task Management

AgentJobs manages all project tasks with structured YAML data.

### Browser GUI

Start the web interface:

\`\`\`bash
agentjobs serve
# Visit http://localhost:8765
\`\`\`

**Features:**
- Dashboard with task counts and spotlight for tasks needing attention
- Task list with search and filters
- Task detail with Human View (summary) vs Agent View (full implementation)
- Dark theme throughout
- Markdown rendering for all task content

### CLI

\`\`\`bash
# List all tasks
agentjobs list

# Show specific task
agentjobs show task-031

# Filter by status
agentjobs list --status in_progress

# Filter by priority
agentjobs list --priority high

# Create new task
agentjobs create --title "New Feature" --priority high --category infrastructure
\`\`\`

### Python Client (For AI Agents)

\`\`\`python
from agentjobs import TaskClient

client = TaskClient()

# Get next high-priority task
task = client.get_next_task(priority="high")

# Mark in progress
client.mark_in_progress(task.id, agent="codex")

# Get starter prompt
prompt = client.get_starter_prompt(task.id)

# Add progress update
client.add_progress_update(
    task.id,
    summary="Completed Phase 1",
    details="All tests passing, ready for review"
)

# Mark complete
client.mark_completed(task.id, agent="codex")
\`\`\`

### Task File Structure

Tasks stored as YAML in `tasks/` directory:
- `tasks/task-001.yaml`
- `tasks/task-002.yaml`
- etc.

Each task includes:
- **Metadata**: status, priority, category, assigned_to, estimated_effort
- **Content**: human_summary (concise), description (full implementation details)
- **Phases**: progress tracking with status per phase
- **Deliverables**: checklist of files/artifacts to create
- **Status Updates**: timeline of progress by agent/human
- **Prompts**: starter prompt + followup prompts
- **Dependencies**: links to other tasks
- **External Links**: reference docs, PRs, issues

### Human vs Agent Views

**Human View** (👤):
- Concise 1-2 sentence summary
- Progress overview with emoji status indicators
- No implementation details

**Agent View** (🤖):
- Full markdown implementation details
- Code snippets, file paths, technical instructions
- Acceptance criteria and verification steps

**Waiting for Human Status**:
- Orange badge indicates task needs human review/approval
- Dashboard spotlight highlights up to 3 waiting tasks
- Notification badge in nav bar shows count

### Creating Tasks

**Via CLI:**
\`\`\`bash
agentjobs create \\
  --title "Implement Feature X" \\
  --priority high \\
  --category feature \\
  --assigned-to codex
\`\`\`

**Manual YAML:**
Create `tasks/task-XXX.yaml` following the schema (see existing tasks for examples).

**Via Markdown Migration:**
\`\`\`bash
agentjobs migrate \\
  'ops/tasks/new-task.md' \\
  tasks/ \\
  --prompts-dir ops/prompts
\`\`\`

### Legacy System

**Deprecated:** Old markdown tasks in `ops/tasks-archive/*.md`
**Current:** Structured YAML in `tasks/*.yaml`

Use `tasks/*.yaml` going forward.
```

**Update `ALLAGENTS.md` - Add section after "Role Definition":**

```markdown
## Task Workflow (Tasks 2.0)

AgentJobs provides structured task management via Python client.

### Basic Workflow

\`\`\`python
from agentjobs import TaskClient

# Initialize client
client = TaskClient(base_url="http://localhost:8765")  # or default

# 1. Query next task
task = client.get_next_task()
print(f"Working on: {task.title}")

# 2. Get starter prompt
prompt = client.get_starter_prompt(task.id)
print(f"Instructions:\\n{prompt}")

# 3. Mark in progress
client.mark_in_progress(task.id, agent="your-agent-name")

# 4. Work on task...
# (your implementation here)

# 5. Add progress updates
client.add_progress_update(
    task.id,
    summary="Completed Phase 1: Core Implementation",
    details="Files created: src/module.py, tests/test_module.py. All tests passing."
)

# 6. Mark complete
client.mark_completed(task.id, agent="your-agent-name")
\`\`\`

### Advanced Features

\`\`\`python
# Filter tasks
high_priority = client.list_tasks(priority="high")
in_progress = client.list_tasks(status="in_progress")

# Update task status
client.update_status(
    task.id,
    status="blocked",
    summary="Blocked on external dependency",
    details="Waiting for library XYZ to release version 2.0"
)

# Mark deliverable complete
client.mark_deliverable_complete(
    task.id,
    deliverable_path="src/feature.py"
)

# Add followup prompt (for multi-phase tasks)
client.add_followup_prompt(
    task.id,
    content="Phase 2: Optimize performance for large datasets",
    context="Phase 1 complete, moving to optimization"
)
\`\`\`

### Task States

- **planned**: Not started, ready to be picked up
- **in_progress**: Agent actively working
- **waiting_for_human**: Needs human review/approval/decision
- **blocked**: Cannot proceed due to external dependency
- **under_review**: Code review in progress
- **completed**: Work finished and verified
- **archived**: No longer relevant

### Human View vs Agent View

When browsing tasks in GUI:
- **Human View**: Read concise `human_summary` for quick understanding
- **Agent View**: Read full `description` with implementation details

As an agent, always use `get_starter_prompt()` to get your working instructions.
```

---

### 8. Archive Legacy Tasks (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Create archive directory
mkdir -p ops/tasks-archive

# Move old markdown tasks
mv ops/tasks/task-*.md ops/tasks-archive/
# Should move 34 files

# Verify ops/tasks is empty
ls ops/tasks/
# Should show nothing (or just README if one exists)

# Update ops/TASKS.md
cat > ops/TASKS.md << 'EOF'
# Tasks

**Tasks 2.0:** All tasks now managed via AgentJobs.

## View Tasks

**Browser GUI:**
\`\`\`bash
agentjobs serve
# Visit http://localhost:8765
\`\`\`

**CLI:**
\`\`\`bash
agentjobs list                     # List all tasks
agentjobs show task-031            # Show specific task
agentjobs list --status in_progress  # Filter by status
\`\`\`

## Features

- **Dashboard**: Task counts, spotlight for tasks needing attention
- **Human/Agent Views**: Separate concise summaries from technical details
- **Waiting for Human**: Orange status for tasks needing review/approval
- **Markdown Rendering**: Full GitHub-flavored markdown support
- **Search & Filters**: Find tasks by status, priority, category

## Legacy Tasks

Old markdown tasks archived in \`ops/tasks-archive/*.md\`.

See \`ENGINEERING.md\` for full Tasks 2.0 documentation.
EOF
```

---

### 9. Update .gitignore (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

# Add AgentJobs runtime cache to .gitignore
echo "" >> .gitignore
echo "# AgentJobs runtime cache" >> .gitignore
echo ".agentjobs/" >> .gitignore

# Note: tasks/ directory SHOULD be version controlled
# (YAML files are human-readable and git-friendly)
```

---

### 10. Commit Everything (privateproject repo)

```bash
cd /mnt/c/projects/privateproject

git add -A
git status
# Should show:
# - Modified: pyproject.toml (if added dependency)
# - Modified: ENGINEERING.md, ALLAGENTS.md, ops/TASKS.md
# - Modified: .gitignore
# - Added: tasks/ (34 YAML files)
# - Deleted: ops/tasks/*.md (34 files moved to ops/tasks-archive/)
# - Untracked: ops/tasks-archive/ (34 archived markdown files)

# Add tasks-archive to git (for historical reference)
git add ops/tasks-archive/

git commit -m "feat(tasks): integrate AgentJobs Tasks 2.0 system

Migrate from markdown task files to structured YAML with AgentJobs.

## Changes

**Integration:**
- Add agentjobs as editable dependency
- Install agentjobs package from ../agentjobs
- Migrate 34 tasks from ops/tasks/*.md to tasks/*.yaml

**Documentation:**
- Update ENGINEERING.md with Tasks 2.0 workflows
- Update ALLAGENTS.md with Python client examples
- Update ops/TASKS.md to point to new system
- Add .agentjobs/ to .gitignore (runtime cache)

**Task Migration:**
- All 34 legacy markdown tasks converted to YAML
- Human summaries extracted automatically
- Prompts preserved from ops/prompts/*.md
- Archive original markdown in ops/tasks-archive/

## Features

Tasks 2.0 provides:
- Browser GUI with dark theme
- Human View (summaries) vs Agent View (implementation)
- Waiting-for-human status with dashboard spotlight
- REST API for programmatic access
- Python client for AI agents
- Markdown rendering (headings, code, bullets)
- Search and filters by status/priority
- Structured YAML (phases, deliverables, prompts)

## Testing

✅ All 34 tasks migrated successfully
✅ Browser GUI displays all tasks correctly
✅ Human/Agent views render properly
✅ CLI commands work (list, show, create)
✅ Python client tested
✅ Markdown renders correctly

## Usage

\`\`\`bash
# Start web interface
agentjobs serve

# Open http://localhost:8765
\`\`\`

Closes #031"

git push origin main
```

---

## Acceptance Criteria

**Installation:**
- [ ] AgentJobs installed in privateproject venv
- [ ] `agentjobs --version` works
- [ ] Sample test data loads successfully

**Migration:**
- [ ] All 34 tasks migrated to YAML
- [ ] No migration errors or warnings
- [ ] Human summaries extracted for all tasks
- [ ] Prompts preserved from ops/prompts/

**Testing:**
- [ ] Browser GUI shows all 34 tasks
- [ ] Task detail pages render correctly
- [ ] Human View shows concise summaries
- [ ] Agent View shows full implementation
- [ ] Markdown renders (headings, bullets, code)
- [ ] Search and filters work
- [ ] Phases display with status
- [ ] Deliverables show
- [ ] CLI commands work

**Documentation:**
- [ ] ENGINEERING.md updated
- [ ] ALLAGENTS.md updated
- [ ] ops/TASKS.md updated
- [ ] .gitignore includes .agentjobs/

**Cleanup:**
- [ ] Legacy tasks archived in ops/tasks-archive/
- [ ] ops/tasks/ is empty (or just README)
- [ ] tasks/ is version controlled (not gitignored)
- [ ] Everything committed and pushed

---

## Rollback Plan

If issues arise:

```bash
cd /mnt/c/projects/privateproject

# Restore legacy markdown tasks
mv ops/tasks-archive/*.md ops/tasks/

# Remove migrated YAML
rm -rf tasks/

# Uninstall agentjobs
.venv/bin/pip uninstall agentjobs -y

# Revert documentation
git restore ENGINEERING.md ALLAGENTS.md ops/TASKS.md .gitignore pyproject.toml

# Remove tasks-archive
rm -rf ops/tasks-archive/
```

---

## Notes

**Why tasks/ should be version controlled:**
- YAML is human-readable and git-friendly
- Enables collaboration (see task changes in PRs)
- Provides history of task evolution
- No binary data (unlike .agentjobs/agentjobs.db cache)

**What to .gitignore:**
- `.agentjobs/agentjobs.db` (SQLite cache, auto-regenerated)
- `.agentjobs/*.log` (runtime logs)

**Prompt files:**
- Keep existing `ops/prompts/*.md` structure
- Migration automatically links them into YAML `prompts.starter` field
- No need to move or rename

**Post-integration:**
- All new tasks created via `agentjobs create` or manual YAML
- No more markdown task files
- Agents use `TaskClient` for programmatic access
- Humans use browser GUI or CLI

---

## Verification Commands

After completing all steps, verify everything works:

```bash
cd /mnt/c/projects/privateproject

# Check installation
.venv/bin/agentjobs --version

# Count migrated tasks
ls tasks/*.yaml | wc -l
# Should show: 34

# Start server
.venv/bin/agentjobs serve &
SERVER_PID=$!

# Test Python client
.venv/bin/python << 'EOF'
from agentjobs import TaskClient
client = TaskClient()
tasks = client.list_tasks()
print(f"✓ {len(tasks)} tasks loaded")
assert len(tasks) == 34, f"Expected 34 tasks, got {len(tasks)}"
task = client.get_next_task()
print(f"✓ Next task: {task.title}")
prompt = client.get_starter_prompt(task.id)
print(f"✓ Prompt length: {len(prompt)} chars")
print("✅ All client tests passed")
EOF

# Stop server
kill $SERVER_PID

# Check git status
git status
# Should be clean after commit

echo "✅ Phase 5.1 Complete!"
```

Ready to integrate! 🔗
