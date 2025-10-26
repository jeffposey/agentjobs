#!/usr/bin/env python
"""Continuous agent: Keep processing tasks until none available."""

from __future__ import annotations

import sys
import time
from pathlib import Path


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import TaskClient, TaskStatus

AGENT_NAME = "continuous-agent"
POLL_INTERVAL = 30  # seconds


def process_task(client: TaskClient, task) -> None:
    """Process a single task end-to-end."""
    print(f"\n[{AGENT_NAME}] Working on: {task.title}")

    client.mark_in_progress(task.id, agent=AGENT_NAME)

    prompt = client.get_starter_prompt(task.id)
    preview = prompt.replace("\n", " ")[:100]
    print(f"Instructions: {preview}{'...' if len(prompt) > 100 else ''}")

    # TODO: Replace with your implementation
    time.sleep(2)  # Simulate work

    client.add_progress_update(
        task.id,
        summary="Completed successfully",
        agent=AGENT_NAME,
    )
    client.mark_completed(task.id, agent=AGENT_NAME)
    print(f"[{AGENT_NAME}] ✅ Completed: {task.title}")


def main() -> None:
    """Continuously poll for work until interrupted."""
    client = TaskClient()
    tasks_completed = 0

    print(f"[{AGENT_NAME}] Starting continuous task processing...")
    print(f"[{AGENT_NAME}] Poll interval: {POLL_INTERVAL}s")

    try:
        while True:
            task = client.get_next_task()

            if task:
                process_task(client, task)
                tasks_completed += 1
            else:
                print(f"[{AGENT_NAME}] No tasks available, waiting...")
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n[{AGENT_NAME}] Shutting down...")
        active = client.list_tasks(status=TaskStatus.IN_PROGRESS)
        print(f"[{AGENT_NAME}] Tasks currently in progress: {len(active)}")
        print(f"[{AGENT_NAME}] Total tasks completed: {tasks_completed}")


if __name__ == "__main__":
    main()
