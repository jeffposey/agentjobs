"""Round-trip corpus test: the v2 model must load every task YAML in the repo.

This is the safety net for the schema migration (task-052). Any change to
src/agentjobs/models_v2.py that breaks loading of an existing task file fails here
with the exact file and validation error, and any file that lost its `schema: 2`
stamp is named rather than silently treated as v1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from agentjobs.models_v2 import SCHEMA_VERSION, load_task

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
def test_task_yaml_is_stamped_and_round_trips(path: Path) -> None:
    """Every task file must carry the stamp, validate, and round-trip losslessly."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data, f"{path} parsed to an empty document"
    assert data.get("schema") == SCHEMA_VERSION, (
        f"{path.name} is not stamped 'schema: {SCHEMA_VERSION}' -- an unstamped file "
        "is treated as v1 and refused by the loader"
    )

    task = load_task(data, source=path.name)

    dumped = task.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
    )
    reparsed = load_task(dumped, source=path.name)
    assert (
        reparsed.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"display_status"}
        )
        == dumped
    ), f"{path.name} does not survive a serialize/deserialize round trip"
