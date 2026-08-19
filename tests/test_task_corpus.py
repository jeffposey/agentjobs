"""Round-trip corpus test: the v2 model must load every task YAML in the repo.

This is the safety net for the schema migration (task-052). Any change to
src/agentjobs/models_v2.py that breaks loading of an existing task file fails here
with the exact file and validation error, and any file that lost its `schema: 2`
stamp is named rather than silently treated as v1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator

import pytest
import yaml

from agentjobs.models_v2 import DeliverableStatus, Lifecycle, SCHEMA_VERSION, Task, load_task

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("tasks/agentjobs", "tasks/test-data")


def corpus_files() -> Iterator[Path]:
    """Yield every task YAML file tracked as part of the corpus."""
    for rel in CORPUS_DIRS:
        yield from sorted((REPO_ROOT / rel).glob("*.yaml"))


def is_absolute(path: str) -> bool:
    """True for a path anchored outside the repository, in either OS's spelling."""
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def ignored_by_git(paths: set[str]) -> set[str]:
    """Return the subset of `paths` git ignores, i.e. generated rather than checked in.

    Deliberately byte-oriented: `text=True` would translate each `\\n` to `\\r\\n` on
    Windows, git would receive every path with a trailing carriage return, and it
    would echo back names that match nothing here. Falls back to treating nothing as
    generated when git cannot answer -- outside a checkout, the stricter behaviour is
    the safe one.
    """
    candidates = sorted(path for path in paths if not is_absolute(path))
    if not candidates:
        return set()

    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(candidates).encode("utf-8"),
        cwd=REPO_ROOT,
        capture_output=True,
    )
    # 0: at least one path is ignored. 1: none are. Anything else: git could not answer.
    if result.returncode not in (0, 1):
        return set()

    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    return {line.strip().replace("\\", "/") for line in lines if line.strip()}


def agentjobs_tasks() -> list[Task]:
    """Load the complete product corpus for relationship and currency checks."""
    return [
        load_task(
            yaml.safe_load(path.read_text(encoding="utf-8")),
            source=path.name,
        )
        for path in sorted((REPO_ROOT / "tasks" / "agentjobs").glob("*.yaml"))
    ]


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


def test_agentjobs_task_ids_and_relationships_are_not_dangling() -> None:
    """The durable roadmap must not point at records that do not exist."""
    tasks = agentjobs_tasks()
    ids = [task.id for task in tasks]
    assert len(ids) == len(set(ids)), "AgentJobs task ids must be unique"

    known = set(ids)
    for task in tasks:
        if task.parent is not None:
            assert task.parent in known, f"{task.id} has missing parent {task.parent}"
        for dependency in task.dependencies:
            assert dependency.task in known, (
                f"{task.id} has missing {dependency.type.value} dependency " f"{dependency.task}"
            )


def test_agentjobs_context_paths_exist() -> None:
    """Read-this-first pointers are useful only while their target still exists.

    A task's own pending deliverables are exempt. A task may legitimately point at a
    file it exists in order to create -- task-002 does exactly that, naming an
    untracked plugin manifest as both the thing to read and the thing to produce.
    Requiring it to exist made this test pass only in a clone where someone had
    already created the file by hand, and fail in every worktree and every fresh
    clone. That is a test asserting the state of one developer's disk rather than the
    state of the repository.

    Generated output is exempt for the same reason, arriving by a different door:
    `src/agentjobs/frontend_dist/` exists only after `npm run build`, so a pointer at
    it passed in a clone where someone had built the frontend and failed in every
    fresh worktree. Worse, the gate builds it -- so the first run failed and the
    second passed.

    Every bad pointer is reported at once. Stopping at the first turns corpus rot into
    one fix per full gate run, and the gate takes four and a half minutes.
    """
    pointers: list[tuple[str, str]] = []
    for task in agentjobs_tasks():
        pending_deliverables = {
            deliverable.path.rstrip("/")
            for deliverable in task.deliverables
            if deliverable.status is not DeliverableStatus.DONE
        }
        for pointer in task.spec.context:
            path = pointer.path.rstrip("/")
            if "://" in path or any(character in path for character in "*{}<>"):
                continue
            if path in pending_deliverables:
                continue
            # An absolute path is the validator's `absolute-path` rule to report, and
            # checking it here would ask whether one machine happens to have that
            # directory -- `REPO_ROOT / "C:/elsewhere"` resolves to `C:/elsewhere`.
            if is_absolute(path):
                continue
            pointers.append((task.id, path))

    generated = ignored_by_git({path for _, path in pointers})
    missing = [
        f"{task_id} -> {path}"
        for task_id, path in pointers
        if path not in generated and not (REPO_ROOT / path).exists()
    ]

    assert not missing, "context pointers name paths that do not exist: " + ", ".join(missing)


def test_open_ui_tasks_do_not_target_legacy_templates() -> None:
    """New product UI work belongs to React; Jinja is compatibility/history only."""
    for task in agentjobs_tasks():
        if task.lifecycle is Lifecycle.CLOSED:
            continue

        paths = [pointer.path for pointer in task.spec.context]
        paths.extend(deliverable.path for deliverable in task.deliverables)
        assert not any(
            path.startswith("src/agentjobs/api/templates") for path in paths
        ), f"{task.id} still directs open UI work to the legacy template tree"

        current_summary = task.spec.summary.lower()
        assert (
            "web ui is server-rendered" not in current_summary
        ), f"{task.id} presents server rendering as the current UI"
