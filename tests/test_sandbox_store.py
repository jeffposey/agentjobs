"""The review sandboxes serve what they seed (task-427).

Every ``scripts/*_sandbox.py`` seeds a throwaway project and then serves it. From
2026-09-10 until this test existed, every one of them seeded ``local-tasks-<hash>.db``
and served ``<project_id>.db``: the page said "No tasks yet" under a list of seeded task
URLs, and no test noticed because none built a sandbox and read it back. So the first
case here does exactly that, through the server's own route, and the rest pin the two
refusals that make the mistake impossible to repeat quietly.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.projects import HOME_ENV, ProjectRegistry
from agentjobs.storage_config import DATABASE_ENV
from agentjobs.store_factory import server_process, task_manager_for

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SANDBOXES = sorted(SCRIPTS.glob("*_sandbox.py"))


@pytest.fixture()
def script() -> Iterator[Callable[[str], ModuleType]]:
    """Import a module from scripts/, the way a sandbox run from that directory does."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield importlib.import_module
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture()
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.delenv(DATABASE_ENV, raising=False)
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()
    yield home
    reset_dependency_cache()


def test_a_built_sandbox_serves_the_tasks_it_seeded(
    tmp_path: Path, sandbox_home: Path, script: Callable[[str], ModuleType]
) -> None:
    """The review panel sandbox, built and registered exactly the way its main() does."""
    review_panel_sandbox = script("review_panel_sandbox")

    project_id, name = "sandbox-panel", "Sandbox: the review panel"
    root = tmp_path / "sandbox"
    project = ProjectRegistry(sandbox_home).add(
        review_panel_sandbox.build(root, project_id=project_id, name=name),
        project_id=project_id,
        name=name,
    )
    seeded = [task_id for task_id, _state, _note in review_panel_sandbox.STATES]

    with server_process():
        served = task_manager_for(project).list_tasks()
    assert sorted(task.id for task in served) == seeded

    with TestClient(app) as client:
        listing = {p["id"]: p for p in client.get("/api/projects").json()}
    assert listing[project_id]["task_count"] == len(seeded)

    # The fallback file the defect wrote into must not exist at all.
    assert not list((sandbox_home / "databases").glob("local-*.db"))


def test_a_second_sandbox_project_does_not_seed_into_the_first(
    tmp_path: Path, sandbox_home: Path, script: Callable[[str], ModuleType]
) -> None:
    """Build, register, build, register -- the loop every two-project sandbox runs.

    Before task-427 the second build asked the registry which project it was in, got
    "the sole registered project" back, and seeded into the first project's database:
    dispatch_groups_sandbox served the runner project's task on the grouped project's
    page, and an empty runner project.
    """
    review_panel_sandbox = script("review_panel_sandbox")

    registry = ProjectRegistry(sandbox_home)
    root = tmp_path / "sandbox"
    projects = {}
    for project_id in ("sandbox-first", "sandbox-second"):
        projects[project_id] = registry.add(
            review_panel_sandbox.build(root, project_id=project_id, name=project_id),
            project_id=project_id,
            name=project_id,
        )

    seeded = len(review_panel_sandbox.STATES)
    with server_process():
        counts = {pid: len(task_manager_for(p).list_tasks()) for pid, p in projects.items()}
    assert counts == {"sandbox-first": seeded, "sandbox-second": seeded}


def test_sandbox_store_requires_a_project_id(
    sandbox_home: Path, script: Callable[[str], ModuleType]
) -> None:
    store = script("sandbox_store")

    with pytest.raises(TypeError):
        store.sandbox_store(sandbox_home.parent / "p" / "tasks")
    with pytest.raises(store.SandboxStoreError, match="project id"):
        store.sandbox_store(sandbox_home.parent / "p" / "tasks", project_id="")


def test_sandbox_store_refuses_an_unredirected_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Callable[[str], ModuleType]
) -> None:
    store = script("sandbox_store")

    monkeypatch.delenv(HOME_ENV, raising=False)
    with pytest.raises(store.SandboxStoreError, match="not set"):
        store.sandbox_store(tmp_path / "p" / "tasks", project_id="sandbox-x")


def test_sandbox_store_refuses_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Callable[[str], ModuleType]
) -> None:
    store = script("sandbox_store")

    monkeypatch.setenv(HOME_ENV, str(Path.home() / ".agentjobs"))
    with pytest.raises(store.SandboxStoreError, match="real home"):
        store.sandbox_store(tmp_path / "p" / "tasks", project_id="sandbox-x")


def test_sandbox_store_refuses_a_database_override_outside_the_home(
    tmp_path: Path,
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Callable[[str], ModuleType],
) -> None:
    store = script("sandbox_store")

    elsewhere = tmp_path / "elsewhere" / "live.db"
    monkeypatch.setenv(DATABASE_ENV, str(elsewhere))
    with pytest.raises(store.SandboxStoreError, match="outside the sandbox home"):
        store.sandbox_store(tmp_path / "p" / "tasks", project_id="sandbox-x")
    assert not elsewhere.exists()


def test_the_sandbox_list_is_not_empty() -> None:
    """A glob that matched nothing would make every parametrised case below vacuous."""
    assert len(SANDBOXES) >= 20


@pytest.mark.parametrize("script", SANDBOXES, ids=lambda path: path.stem)
def test_no_sandbox_imports_the_package_before_redirecting_its_home(script: Path) -> None:
    """A module-level import runs before main() can point AGENTJOBS_HOME at scratch."""
    scoped = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

    def module_level_imports(node: ast.AST) -> Iterator[str]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scoped):
                continue
            if isinstance(child, ast.Import):
                yield from (alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                yield child.module
            yield from module_level_imports(child)

    tree = ast.parse(script.read_text(encoding="utf-8"))
    offenders = [
        name
        for name in module_level_imports(tree)
        if name.split(".")[0] in {"agentjobs", "sandbox_store"}
    ]
    assert offenders == []


@pytest.mark.parametrize("script", SANDBOXES, ids=lambda path: path.stem)
def test_every_sandbox_seed_names_its_project(script: Path) -> None:
    """The keyword is required, but scripts only find that out when someone runs them."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sandbox_store"
    ]
    assert all(any(k.arg == "project_id" for k in call.keywords) for call in calls)
