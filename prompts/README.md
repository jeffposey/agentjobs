# AgentJobs Prompts

Agent instructions for AgentJobs development tasks.

## Structure

- `task-XXX-phase-Y.md` - Phase-specific prompts
- Each prompt contains detailed implementation instructions
- Prompts are referenced in task YAML files

## Usage

Prompts are automatically linked during migration:

```bash
agentjobs migrate \
  'tasks/agentjobs/task-*.md' \
  tasks/ \
  --prompts-dir prompts
```

Or manually referenced in YAML:

```yaml
prompts:
  starter: prompts/task-032-phase-1.md
  followups:
    - prompt_file: prompts/task-032-phase-2.md
```
