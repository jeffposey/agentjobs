"""``agentjobs branches``: the read-only half of task-293.

Against real git repositories, for the reason the finish suite gives: every interesting
property here is what git says about a branch, and a mocked ``subprocess`` would assert
that the right flags were passed while proving nothing about what they answer.

The one property worth naming up front, because it is the one a reader will doubt: a
rebase preserves author dates and rewrites committer dates, so a branch's reported age
has to come from ``%at``. There is a test below that rebases and checks the age survives.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.branch_report import survey_branches
from agentjobs.cli import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from agentjobs.storage import TaskStorage


def git(root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def commit(root: Path, name: str, *, author_date: str = "") -> None:
    (root / name).write_text(name, encoding="utf-8")
    git(root, "add", "--", name)
    env = ["-c", "user.name=t", "-c", "user.email=t@t.t"]
    args = [*env, "commit", "-m", f"add {name}"]
    if author_date:
        args = [*env, "commit", "--date", author_date, "-m", f"add {name}"]
    git(root, *args)


@pytest.fixture
def repo(tmp_path: Path) -> Dict[str, Any]:
    root = tmp_path / "clone"
    (root / "tasks").mkdir(parents=True)
    git(tmp_path, "init", "--initial-branch=main", str(root))
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    commit(root, "base.txt")
    manager = TaskManager(TaskStorage(root / "tasks"))
    return {"root": root, "manager": manager, "tmp": tmp_path}


def make_branch(repo: Dict[str, Any], name: str, file: str, *, author_date: str = "") -> None:
    """A branch with one commit of its own, made in its own worktree and then released."""
    root: Path = repo["root"]
    worktree: Path = repo["tmp"] / "worktrees" / name.replace("/", "-")
    git(root, "worktree", "add", "-b", name, str(worktree), "main")
    commit(worktree, file, author_date=author_date)
    git(root, "worktree", "remove", str(worktree))


def task_for(repo: Dict[str, Any], branch: str, *, closed: bool = False) -> str:
    manager: TaskManager = repo["manager"]
    task = manager.create_task(
        title=f"Work on {branch}",
        category="infrastructure",
        summary="A task with a branch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="claude")
    manager.update_task(task.id, actor="claude", branches=[{"name": branch, "status": "active"}])
    if closed:
        from agentjobs.models_v2 import Outcome

        manager.close_task(task.id, actor="claude", outcome=Outcome.COMPLETED)
    return task.id


class TestWhatItSees:
    def test_a_merged_branch_with_no_worktree_is_litter(self, repo: Dict[str, Any]) -> None:
        make_branch(repo, "feat/task-001-done", "done.txt")
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-001-done")

        report = survey_branches(repo["root"], repo["manager"])

        assert [row.name for row in report.litter] == ["feat/task-001-done"]
        assert report.in_flight == []

    def test_an_unmerged_branch_is_in_flight_and_never_litter(self, repo: Dict[str, Any]) -> None:
        make_branch(repo, "feat/task-002-open", "open.txt")

        report = survey_branches(repo["root"], repo["manager"])

        assert report.litter == []
        assert [row.name for row in report.in_flight] == ["feat/task-002-open"]

    def test_a_merged_branch_still_in_a_worktree_is_not_offered_for_deletion(
        self, repo: Dict[str, Any]
    ) -> None:
        """Somebody is standing in it. ``git branch -d`` would refuse, and rightly."""
        root = repo["root"]
        worktree = repo["tmp"] / "worktrees" / "held"
        git(root, "worktree", "add", "-b", "feat/task-003-held", str(worktree), "main")
        commit(worktree, "held.txt")
        git(root, "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-003-held")

        report = survey_branches(root, repo["manager"])

        row = next(entry for entry in report.rows if entry.name == "feat/task-003-held")
        assert row.merged
        assert row.worktree == worktree
        assert not row.deletable
        assert report.litter == []

    def test_the_base_is_never_a_row(self, repo: Dict[str, Any]) -> None:
        assert survey_branches(repo["root"], repo["manager"]).rows == []

    def test_it_deletes_nothing(self, repo: Dict[str, Any]) -> None:
        """The whole point. A report that quietly tidied would be a different feature."""
        make_branch(repo, "feat/task-004-safe", "safe.txt")
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-004-safe")

        survey_branches(repo["root"], repo["manager"])

        assert "feat/task-004-safe" in git(repo["root"], "branch", "--list").stdout


class TestWhoseItIs:
    def test_a_branch_is_matched_to_the_task_that_lists_it(self, repo: Dict[str, Any]) -> None:
        make_branch(repo, "feat/task-005-owned", "owned.txt")
        task_id = task_for(repo, "feat/task-005-owned")

        report = survey_branches(repo["root"], repo["manager"])

        row = next(entry for entry in report.rows if entry.name == "feat/task-005-owned")
        assert row.task_id == task_id
        assert "active" in row.task_state

    def test_a_closed_task_still_names_its_branch(self, repo: Dict[str, Any]) -> None:
        """Closed tasks are exactly the ones leftover branches belong to."""
        make_branch(repo, "feat/task-006-closed", "closed.txt")
        task_id = task_for(repo, "feat/task-006-closed", closed=True)
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-006-closed")

        report = survey_branches(repo["root"], repo["manager"])

        row = report.litter[0]
        assert row.task_id == task_id
        assert row.task_state == "closed completed"

    def test_a_branch_no_task_claims_says_so_rather_than_guessing(
        self, repo: Dict[str, Any]
    ) -> None:
        make_branch(repo, "chore/no-task", "loose.txt")

        report = survey_branches(repo["root"], repo["manager"])

        row = next(entry for entry in report.rows if entry.name == "chore/no-task")
        assert row.task_id is None
        assert "no task" in row.task_state


class TestAge:
    def test_age_comes_from_the_author_date_and_survives_a_rebase(
        self, repo: Dict[str, Any]
    ) -> None:
        """``%ct`` would report a branch rebased a moment ago as a moment old.

        That is the number the whole lifetime half of task-293 turns on, so it is the
        one property here worth proving against a real rebase rather than reasoning about.
        """
        root = repo["root"]
        old = time.time() - 9 * 86400
        worktree = repo["tmp"] / "worktrees" / "aged"
        git(root, "worktree", "add", "-b", "feat/task-007-aged", str(worktree), "main")
        commit(worktree, "aged.txt", author_date=f"@{int(old)} +0000")

        before = survey_branches(root, repo["manager"])
        aged = next(row for row in before.rows if row.name == "feat/task-007-aged")
        assert aged.age_days is not None and 8.5 < aged.age_days < 9.5

        # main moves, the branch rebases onto it, every committer date is rewritten.
        commit(root, "meanwhile.txt")
        git(worktree, "rebase", "main")

        after = survey_branches(root, repo["manager"])
        rebased = next(row for row in after.rows if row.name == "feat/task-007-aged")
        assert rebased.age_days is not None and 8.5 < rebased.age_days < 9.5

    def test_a_merged_branch_has_no_age_to_report(self, repo: Dict[str, Any]) -> None:
        """It is not in flight, so "how long has it been open" has no answer."""
        make_branch(repo, "feat/task-008-in", "in.txt")
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-008-in")

        report = survey_branches(repo["root"], repo["manager"])

        assert report.litter[0].age_days is None

    def test_in_flight_is_oldest_first(self, repo: Dict[str, Any]) -> None:
        now = time.time()
        make_branch(repo, "feat/task-009-young", "y.txt", author_date=f"@{int(now - 3600)} +0000")
        make_branch(
            repo, "feat/task-010-old", "o.txt", author_date=f"@{int(now - 5 * 86400)} +0000"
        )

        report = survey_branches(repo["root"], repo["manager"])

        assert [row.name for row in report.in_flight] == [
            "feat/task-010-old",
            "feat/task-009-young",
        ]


class TestTheCommand:
    """``agentjobs branches`` end to end, through the registry the CLI really reads."""

    def _register(self, repo: Dict[str, Any]) -> None:
        import os

        from agentjobs.projects import ProjectRegistry

        root = repo["root"]
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Demo", "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        ProjectRegistry(home=Path(os.environ["AGENTJOBS_HOME"])).add(root, project_id="demo")

    def test_it_names_the_leftovers_and_the_command_that_removes_them(
        self, repo: Dict[str, Any]
    ) -> None:
        self._register(repo)
        make_branch(repo, "feat/task-011-left", "left.txt")
        task_id = task_for(repo, "feat/task-011-left", closed=True)
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-011-left")

        result = CliRunner().invoke(app, ["branches", "--project", "demo"])

        assert result.exit_code == 0, result.output
        assert "feat/task-011-left" in result.output
        assert task_id in result.output
        assert "git branch -d" in result.output
        assert "-D" in result.output  # stated as the thing not to reach for

    def test_a_clean_clone_says_so_rather_than_printing_nothing(self, repo: Dict[str, Any]) -> None:
        self._register(repo)
        make_branch(repo, "feat/task-012-open", "open.txt")

        result = CliRunner().invoke(app, ["branches", "--project", "demo"])

        assert result.exit_code == 0, result.output
        assert "left behind" in result.output
        assert "does not contain" in result.output

    def test_it_exits_zero_even_with_litter(self, repo: Dict[str, Any]) -> None:
        """Untidy is not broken, and a report that fails a script gets suppressed."""
        self._register(repo)
        make_branch(repo, "feat/task-013-left", "l.txt")
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-013-left")

        assert CliRunner().invoke(app, ["branches", "--project", "demo"]).exit_code == 0
