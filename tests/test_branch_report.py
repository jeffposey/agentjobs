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

from agentjobs.branch_report import DESKTOP_LABEL, survey_branches
from agentjobs.cli import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from support import task_store


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
    manager = TaskManager(task_store(root / "tasks", project_id="demo"))
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


class TestDesktopWorktrees:
    """Branches the Claude desktop app and agent view make for themselves.

    They land at ``.claude/worktrees/<name>`` inside the clone on a ``worktree-<name>``
    branch, before the session has read an instruction file -- so the branch carries no
    task id and cannot be made to. Read literally by the rest of this report that is
    in-flight work of unknown origin, which is the reading that gets a branch swept.
    """

    def _desktop_worktree(self, repo, name: str, file: str) -> Path:
        """A branch made the way the desktop app makes one: inside the clone, no task."""
        root: Path = repo["root"]
        worktree = root / ".claude" / "worktrees" / name
        git(root, "worktree", "add", "-b", f"worktree-{name}", str(worktree), "main")
        commit(worktree, file)
        return worktree

    def test_the_branch_prefix_alone_labels_it(self, repo: Dict[str, Any]) -> None:
        """The path signal is absent here: the worktree is in the ordinary place.

        Which is the case after somebody points the desktop app's "Worktree location"
        setting at the conventional directory -- the folder moves, the branch name does
        not.
        """
        make_branch(repo, "worktree-plucky-otter", "elsewhere.txt")

        report = survey_branches(repo["root"], repo["manager"])

        row = next(r for r in report.rows if r.name == "worktree-plucky-otter")
        assert row.desktop is True
        assert row.note == DESKTOP_LABEL

    def test_a_path_inside_dot_claude_labels_it_even_once_the_branch_is_renamed(
        self, repo: Dict[str, Any]
    ) -> None:
        """The branch signal is absent here, because renaming it is what we ask for.

        A session that took our advice and ran ``git branch -m`` is still sitting in a
        worktree inside the clone, and the row should still say where it is working.
        """
        self._desktop_worktree(repo, "brave-heron", "inside.txt")
        git(repo["root"], "branch", "-m", "worktree-brave-heron", "feat/task-042-renamed")

        report = survey_branches(repo["root"], repo["manager"])

        row = next(r for r in report.rows if r.name == "feat/task-042-renamed")
        assert row.desktop is True

    def test_an_ordinary_worktree_is_not_labelled(self, repo: Dict[str, Any]) -> None:
        """The label has to be wrong for the common case or it says nothing."""
        make_branch(repo, "feat/task-041-ordinary", "ordinary.txt")

        report = survey_branches(repo["root"], repo["manager"])

        row = next(r for r in report.rows if r.name == "feat/task-041-ordinary")
        assert row.desktop is False
        assert row.note == ""

    def test_nothing_about_the_label_makes_it_deletable(self, repo: Dict[str, Any]) -> None:
        """Report and tolerate. A labelled branch is somebody working, not litter."""
        self._desktop_worktree(repo, "calm-badger", "live.txt")

        report = survey_branches(repo["root"], repo["manager"])

        assert [r.name for r in report.litter] == []
        assert "worktree-calm-badger" in [r.name for r in report.in_flight]

    def test_the_footnote_counts_only_rows_the_command_printed(self, repo: Dict[str, Any]) -> None:
        """A desktop worktree that has not committed yet appears in neither listing.

        It is not litter -- it is checked out -- and not in flight, because the base
        contains every commit it has. Counting it in a footnote under listings it is
        absent from would send a reader looking for a row that is not there.
        """
        root: Path = repo["root"]
        git(
            root,
            "worktree",
            "add",
            "-b",
            "worktree-silent",
            str(root / ".claude" / "worktrees" / "silent"),
            "main",
        )

        report = survey_branches(root, repo["manager"])

        assert [r.name for r in report.rows if r.desktop] == ["worktree-silent"]
        assert report.desktop == []


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

    def _register(self, repo: Dict[str, Any], monkeypatch=None) -> None:
        import os

        from agentjobs.projects import ProjectRegistry

        root = repo["root"]
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Demo", "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        ProjectRegistry(home=Path(os.environ["AGENTJOBS_HOME"])).add(root, project_id="demo")
        if monkeypatch is not None:
            # `branches` resolves its manager the ordinary way, which outside the server
            # is a service client. What these cases assert is what the command prints
            # about git, so the manager is supplied rather than a server started.
            from agentjobs import cli as cli_module

            monkeypatch.setattr(cli_module, "task_manager_for", lambda project: repo["manager"])

    def test_it_names_the_leftovers_and_the_command_that_removes_them(
        self, repo: Dict[str, Any], monkeypatch
    ) -> None:
        self._register(repo, monkeypatch)
        make_branch(repo, "feat/task-011-left", "left.txt")
        task_id = task_for(repo, "feat/task-011-left", closed=True)
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-011-left")

        result = CliRunner().invoke(app, ["branches", "--project", "demo"])

        assert result.exit_code == 0, result.output
        assert "feat/task-011-left" in result.output
        assert task_id in result.output
        assert "git branch -d" in result.output
        assert "-D" in result.output  # stated as the thing not to reach for

    def test_a_clean_clone_says_so_rather_than_printing_nothing(
        self, repo: Dict[str, Any], monkeypatch
    ) -> None:
        self._register(repo, monkeypatch)
        make_branch(repo, "feat/task-012-open", "open.txt")

        result = CliRunner().invoke(app, ["branches", "--project", "demo"])

        assert result.exit_code == 0, result.output
        assert "left behind" in result.output
        assert "does not contain" in result.output

    def test_it_names_a_desktop_worktree_rather_than_leaving_it_unexplained(
        self, repo: Dict[str, Any], monkeypatch
    ) -> None:
        """What a person actually reads: the row is marked and the mark is explained."""
        self._register(repo, monkeypatch)
        root: Path = repo["root"]
        worktree = root / ".claude" / "worktrees" / "eager-marten"
        git(root, "worktree", "add", "-b", "worktree-eager-marten", str(worktree), "main")
        commit(worktree, "desk.txt")

        result = CliRunner().invoke(app, ["branches", "--project", "demo"])

        assert result.exit_code == 0, result.output
        assert "worktree-eager-marten" in result.output
        assert DESKTOP_LABEL in result.output
        assert "git branch -m" in result.output
        assert "Nothing here removes one" in result.output

    def test_it_exits_zero_even_with_litter(self, repo: Dict[str, Any], monkeypatch) -> None:
        """Untidy is not broken, and a report that fails a script gets suppressed."""
        self._register(repo, monkeypatch)
        make_branch(repo, "feat/task-013-left", "l.txt")
        git(repo["root"], "merge", "--no-ff", "--no-edit", "-m", "merge", "feat/task-013-left")

        assert CliRunner().invoke(app, ["branches", "--project", "demo"]).exit_code == 0
