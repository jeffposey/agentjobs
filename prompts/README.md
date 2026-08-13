# Historical AgentJobs Prompts

These files preserve implementation prompts from AgentJobs' earlier phased roadmap.
They are historical records, not current product or contributor documentation. Current
work is specified in schema-v2 task YAML and resumed through `spec`, `ball_prompt`, and
the typed `log`; see [`docs/agent-workflow.md`](../docs/agent-workflow.md).

## Structure

- `task-XXX-phase-Y.md` - Phase-specific prompts
- Each prompt contains detailed implementation instructions
- Prompts are referenced in task YAML files

## Usage

The examples below document the retired schema-v1 migration format and are retained
only to explain the archived files:

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
