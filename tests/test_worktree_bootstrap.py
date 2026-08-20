"""A fresh worktree must be able to verify its own work, and be told how.

`git worktree add` copies tracked files only, so a new worktree has no virtualenv and
no `frontend/node_modules`. Agents are required to take a worktree as their first act,
which made the setup an improvisation; the improvisation on record was borrowing the
main clone's environment, which silently tests the wrong source.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a repository script without making scripts/ an installed package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load_script("check")
setup_problems = check.setup_problems


def make_checkout(root: Path, *, node_modules: bool = True) -> Path:
    """Build the shape of a bootstrapped checkout and return its package file."""
    package = root / "src" / "agentjobs" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    if node_modules:
        (root / "frontend" / "node_modules").mkdir(parents=True)
    return package


def test_a_bootstrapped_checkout_reports_nothing(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)

    assert setup_problems(tmp_path, package) == []


def test_an_uninstalled_package_is_reported(tmp_path: Path) -> None:
    make_checkout(tmp_path)

    assert setup_problems(tmp_path, None) == ["the agentjobs package is not installed"]


def test_importing_another_checkout_is_reported(tmp_path: Path) -> None:
    """The failure this guard exists for: green tests that ran someone else's source.

    A worktree borrowing the main clone's Poetry environment imports that clone's
    `src/`. Everything succeeds and the result describes code the branch does not
    contain, so the gate has to refuse rather than pass.
    """
    worktree = tmp_path / "aj-045"
    worktree.mkdir()
    make_checkout(worktree)
    elsewhere = make_checkout(tmp_path / "main-clone")

    problems = setup_problems(worktree, elsewhere)

    assert len(problems) == 1
    assert "outside this checkout" in problems[0]
    assert str(elsewhere.parent) in problems[0]


def test_missing_node_modules_is_reported(tmp_path: Path) -> None:
    package = make_checkout(tmp_path, node_modules=False)

    assert setup_problems(tmp_path, package) == ["frontend/node_modules is missing"]


def test_the_bootstrap_is_documented_where_worktrees_are_required() -> None:
    """An instruction to take a worktree that omits the bootstrap strands the reader."""
    assert (ROOT / "scripts" / "bootstrap.py").is_file()

    all_agents = (ROOT / "ALLAGENTS.md").read_text(encoding="utf-8")
    engineering = (ROOT / "ENGINEERING.md").read_text(encoding="utf-8")

    assert "### Bootstrapping a worktree" in all_agents
    assert "python scripts/bootstrap.py" in all_agents
    assert "python scripts/bootstrap.py" in engineering
    # Beside the worktree commands, not only in the Setup section a worktree skips.
    assert "git worktree add ../worktrees/aj-045" in engineering
    worktree_block = engineering.split("git worktree add ../worktrees/aj-045", 1)[1]
    assert "scripts/bootstrap.py" in worktree_block.split("```", 1)[0]
