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

from agentjobs import Lifecycle, TaskClient

AGENTS = ("lead-agent", "support-agent", "qa-agent")


def cycle_agents(agents: Iterable[str]) -> Iterable[str]:
    """Return an infinite iterator cycling over agent names."""
    return itertools.cycle(agents)


def main() -> None:
    """Assign multiple tasks to different agents in a round-robin fashion."""
    client = TaskClient()
    ready_tasks = client.list_tasks(lifecycle=Lifecycle.READY)

    if not ready_tasks:
        print("No ready tasks available for assignment.")
        return

    agent_cycle = iter(cycle_agents(AGENTS))

    for task in ready_tasks:
        agent = next(agent_cycle)

        print(f"\nAssigning '{task.title}' to {agent}")
        client.claim_task(task.id, agent=agent)

        spec = task.spec.description
        print(f"Priority: {task.priority.value}")
        print(f"Instructions preview: {spec[:120]}{'...' if len(spec) > 120 else ''}")

        client.add_progress_update(
            task.id,
            agent=agent,
            summary="Initial analysis complete",
            details="Collaborator assigned. Ready for next phase.",
        )

    print("\n✅ All ready tasks assigned to collaborators.")


if __name__ == "__main__":
    main()
