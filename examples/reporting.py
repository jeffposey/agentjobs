#!/usr/bin/env python
"""Generate lightweight status reports for AgentJobs."""

from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import Counter


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import TaskClient


def main() -> None:
    """Produce a status summary across all tasks."""
    client = TaskClient()
    tasks = client.list_tasks()
    if not tasks:
        print("No tasks found.")
        return

    by_status: Counter[str] = collections.Counter(task.status for task in tasks)
    by_priority: Counter[str] = collections.Counter(task.priority for task in tasks)

    print("Task Status Overview")
    print("--------------------")
    for status, count in sorted(
        by_status.items(),
        key=lambda item: str(getattr(item[0], "value", item[0])),
    ):
        label = status.value if hasattr(status, "value") else str(status)
        print(f"{label:>20}: {count}")

    print("\nTask Priority Distribution")
    print("--------------------------")
    for priority, count in sorted(
        by_priority.items(),
        key=lambda item: str(getattr(item[0], "value", item[0])),
    ):
        label = priority.value if hasattr(priority, "value") else str(priority)
        print(f"{label:>20}: {count}")


if __name__ == "__main__":
    main()
