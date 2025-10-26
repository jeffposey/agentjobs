#!/usr/bin/env python
"""Import tasks from an external CSV source."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Iterable, List, MutableMapping


# Allow running from repository checkout without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agentjobs import Priority, TaskClient
from agentjobs.client import TaskClientError

DATA_FILE = Path(__file__).with_name("bulk_tasks.csv")

FALLBACK_DATA = [
    {
        "title": "Refine onboarding checklist",
        "description": "Audit the onboarding flow and add missing steps.",
        "priority": "medium",
        "category": "documentation",
    },
    {
        "title": "Evaluate caching strategy",
        "description": "Compare Redis and Memcached performance metrics.",
        "priority": "high",
        "category": "performance",
    },
]


def load_task_specs(file_path: Path) -> List[MutableMapping[str, str]]:
    """Load task definitions from a CSV file or fall back to in-memory data."""
    if not file_path.exists():
        print(f"⚠️  {file_path.name} not found, using fallback dataset.")
        return [dict(item) for item in FALLBACK_DATA]

    with file_path.open(mode="r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader if row.get("title")]
    return rows


def normalise_priority(value: str) -> Priority | str:
    """Convert CSV priority column to Priority enum when possible."""
    if not value:
        return Priority.MEDIUM
    try:
        return Priority(value.lower())
    except ValueError:
        return value


def import_tasks(client: TaskClient, specs: Iterable[MutableMapping[str, str]]) -> None:
    """Create tasks defined in the CSV against the AgentJobs API."""
    created = 0
    for spec in specs:
        payload = {
            "title": spec["title"],
            "description": spec.get("description", ""),
            "priority": normalise_priority(spec.get("priority", "")),
            "category": spec.get("category") or "general",
        }
        print(f"Importing: {payload['title']} ({payload['priority']})")
        try:
            client.create_task(**payload)
        except TaskClientError as exc:
            print(f"  ✗ Failed: {exc}")
            continue
        created += 1
        print("  ✓ Created")

    print(f"\n✅ Imported {created} tasks.")


def main() -> None:
    """Entry point for CSV-based imports."""
    specs = load_task_specs(DATA_FILE)
    if not specs:
        print("No tasks to import.")
        return

    client = TaskClient()
    import_tasks(client, specs)


if __name__ == "__main__":
    main()
