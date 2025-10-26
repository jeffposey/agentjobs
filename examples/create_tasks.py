#!/usr/bin/env python
"""Create tasks programmatically via the AgentJobs REST API."""

from __future__ import annotations

import sys
from pathlib import Path


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import Priority, TaskClient
from agentjobs.client import TaskClientError

TASK_SPECS = [
    {
        "title": "Draft onboarding guide",
        "description": "Create a markdown guide for new contributors.",
        "priority": Priority.MEDIUM,
        "category": "documentation",
    },
    {
        "title": "Add metrics dashboard",
        "description": "Integrate Prometheus metrics into the API service.",
        "priority": Priority.HIGH,
        "category": "infrastructure",
    },
]


def main() -> None:
    """Create predefined tasks on the AgentJobs server."""
    client = TaskClient()
    created = 0

    for spec in TASK_SPECS:
        print(f"Creating task: {spec['title']}")
        try:
            task = client.create_task(**spec)
        except TaskClientError as exc:
            print(f"  ✗ Failed: {exc}")
            continue
        priority = task.priority.value if hasattr(task.priority, "value") else task.priority
        print(f"  ✓ Created {task.id} (priority={priority})")
        created += 1

    print(f"\n✅ Finished creating tasks ({created} successful).")


if __name__ == "__main__":
    main()
