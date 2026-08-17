"""The initialization instructions the MCP server publishes to every client.

The first paragraph is the accepted wording from section 4 of
``docs/mcp-integration-design.md``. It leads because some clients truncate
instructions, and the rule that matters most has to survive the truncation.
"""

from __future__ import annotations

#: Clients are known to show or inject only a prefix of the instructions. The design
#: fixes 512 characters as the budget the leading rule must fit inside, and a test
#: asserts it.
LEADING_RULE_BUDGET = 512

SERVER_INSTRUCTIONS = """\
AgentJobs task YAML is generated state. Use these tools for every task mutation. \
Call `projects_list`, pass its `project_id` to every task tool, and use only claim, \
handoff, release, and close to move workflow state. Reading task YAML is allowed.

Every mutating tool needs an `actor` from the project's configured vocabulary and a \
caller-generated `operation_id` UUID; reusing an operation_id replays the original \
result instead of writing twice. There is no generic status, lifecycle, or YAML \
setter, and none is coming -- a task moves through the domain verbs or not at all.

Read `task_get` before resuming work. It returns the whole record, which is designed \
to be sufficient working memory for a session with no other context: the spec, the \
current `ball_prompt`, binding decisions, open questions, and dependencies.\
"""
