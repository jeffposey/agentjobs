#!/usr/bin/env python
"""Basic agent workflow: Get task, work on it, complete it."""

from __future__ import annotations

import sys
from pathlib import Path


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import TaskClient

# Configuration
AGENT_NAME = "example-agent"


def main() -> None:
    """Run a one-off task workflow."""
    client = TaskClient(base_url="http://localhost:8765")

    print(f"[{AGENT_NAME}] Looking for next task...")
    task = client.get_next_task(priority="high")

    if not task:
        print(f"[{AGENT_NAME}] No tasks available")
        return

    print(f"[{AGENT_NAME}] Found task: {task.title}")
    print(f"[{AGENT_NAME}] Priority: {task.priority}, Status: {task.status}")

    print(f"[{AGENT_NAME}] Taking ownership...")
    client.mark_in_progress(task.id, agent=AGENT_NAME)

    prompt = client.get_starter_prompt(task.id)
    divider = "=" * 60
    print(f"\n{divider}")
    print(f"TASK: {task.title}")
    print(divider)
    print(f"\n{prompt}\n")
    print(f"{divider}\n")

    print(f"[{AGENT_NAME}] Working on task...")
    # TODO: Implement task-specific logic here

    client.add_progress_update(
        task.id,
        summary="Task completed",
        details="All deliverables finished",
        agent=AGENT_NAME,
    )

    print(f"[{AGENT_NAME}] Marking task complete...")
    client.mark_completed(task.id, agent=AGENT_NAME)

    print(f"[{AGENT_NAME}] ✅ Task {task.id} complete!")


if __name__ == "__main__":
    main()
