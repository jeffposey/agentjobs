#!/usr/bin/env python
"""Collaborative agent example coordinating multiple worker identities."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Iterable


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import Priority, TaskClient, TaskStatus

AGENTS = ("lead-agent", "support-agent", "qa-agent")


def cycle_agents(agents: Iterable[str]) -> Iterable[str]:
    """Return an infinite iterator cycling over agent names."""
    return itertools.cycle(agents)


def main() -> None:
    """Assign multiple tasks to different agents in a round-robin fashion."""
    client = TaskClient()
    planned_tasks = client.list_tasks(status=TaskStatus.PLANNED)

    if not planned_tasks:
        print("No planned tasks available for assignment.")
        return

    agent_cycle = cycle_agents(AGENTS)

    for task in planned_tasks:
        agent = next(agent_cycle)

        print(f"\nAssigning '{task.title}' to {agent}")
        client.mark_in_progress(
            task.id,
            agent=agent,
            summary=f"Picked up by {agent}",
        )

        starter = client.get_starter_prompt(task.id)
        priority = task.priority.value if isinstance(task.priority, Priority) else task.priority
        print(f"Priority: {priority}")
        print(f"Instructions preview: {starter[:120]}{'...' if len(starter) > 120 else ''}")

        client.add_progress_update(
            task.id,
            agent=agent,
            summary="Initial analysis complete",
            details="Collaborator assigned. Ready for next phase.",
        )

    print("\n✅ All planned tasks assigned to collaborators.")


if __name__ == "__main__":
    main()
