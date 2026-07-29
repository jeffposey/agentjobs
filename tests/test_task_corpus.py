"""Round-trip corpus test: the model must load every task YAML in the repo.

This is the safety net for the schema design pass (task-048). Any change to
src/agentjobs/models.py that breaks loading of an existing task file fails here
with the exact file and validation error, instead of the file silently
disappearing from listings (TaskStorage.load_task swallows validation errors
and returns None).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from agentjobs.models import Task

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("tasks/agentjobs", "tasks/test-data")


def corpus_files() -> Iterator[Path]:
    """Yield every task YAML file tracked as part of the corpus."""
    for rel in CORPUS_DIRS:
        yield from sorted((REPO_ROOT / rel).glob("*.yaml"))


def test_corpus_is_not_empty() -> None:
    """Guard against directory moves silently emptying the corpus."""
    files = list(corpus_files())
    assert len(files) >= 20, (
        f"expected the task corpus to contain at least 20 files, found {len(files)} -- "
        "did a tasks directory move without this test being updated?"
    )


@pytest.mark.parametrize("path", corpus_files(), ids=lambda p: p.name)
def test_task_yaml_loads_and_round_trips(path: Path) -> None:
    """Every task file must validate, serialize, and re-validate losslessly."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data, f"{path} parsed to an empty document"

    task = Task.model_validate(data)

    dumped = task.model_dump(mode="json", exclude_none=True)
    reparsed = Task.model_validate(dumped)
    assert reparsed.model_dump(mode="json", exclude_none=True) == dumped, (
        f"{path.name} does not survive a serialize/deserialize round trip"
    )
