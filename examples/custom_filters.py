#!/usr/bin/env python
"""Query tasks with custom filters."""

from __future__ import annotations

import sys
from pathlib import Path


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import Priority, TaskClient, TaskStatus


def main() -> None:
    """Demonstrate querying tasks with different filters."""
    client = TaskClient()

    high_priority = client.list_tasks(priority=Priority.HIGH)
    print(f"High priority tasks: {len(high_priority)}")

    in_progress = client.list_tasks(status=TaskStatus.IN_PROGRESS)
    print(f"In progress: {len(in_progress)}")

    blocked = client.list_tasks(status=TaskStatus.BLOCKED)
    print(f"Blocked: {len(blocked)}")
    for task in blocked:
        print(f"  - {task.title}")

    results = client.search_tasks("database")
    print(f"\nSearch 'database': {len(results)} results")
    for task in results:
        print(f"  - {task.title}")


if __name__ == "__main__":
    main()
