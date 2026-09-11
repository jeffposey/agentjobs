"""The gate script around the projection: what it refuses, and what it declines to judge.

``tests/test_roadmap.py`` covers the rendering. What is left here is the part a gate
stage depends on and a unit test of the renderer cannot reach: that the check compares
against the working tree, that it distinguishes "stale" from "cannot tell", and that a
worktree resolves the same project its clone does.

That last one is not a detail. Every branch in this repository is worked from
``worktrees/agentjobs-NNN`` beside the clone, which is inside no registered project, so a
resolution from the working directory finds nothing and the stage would report "cannot be
verified" in exactly the checkouts where it needs to be real.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> Any:
    """Import the script by path, the way ``check.py`` runs it.

    It lives in ``scripts/`` rather than in the package, so there is no importable name
    for it and the suite has to reach it the same way the gate does.
    """
    spec = importlib.util.spec_from_file_location(
        "export_roadmap", ROOT / "scripts" / "export_roadmap.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_roadmap"] = module
    spec.loader.exec_module(module)
    return module


export_roadmap = _load()


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    """A git repository with one commit, so ``rev-parse`` has something to answer."""
    root = tmp_path / "clone"
    root.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=root, check=True, capture_output=True)
    return root


class TestRepositoryRoot:
    def test_a_worktree_resolves_to_the_clone_that_owns_it(self, clone: Path) -> None:
        """The case every branch in this repository is actually in.

        A worktree is not inside the clone, so without this the project lookup fails and
        the stage silently downgrades to "cannot be verified" on every branch.
        """
        worktree = clone.parent / "worktrees" / "clone-042"
        subprocess.run(
            ["git", "worktree", "add", "-b", "feat/x", str(worktree)],
            cwd=clone,
            check=True,
            capture_output=True,
        )

        assert export_roadmap.repository_root(worktree) == clone.resolve()

    def test_a_plain_clone_resolves_to_itself(self, clone: Path) -> None:
        assert export_roadmap.repository_root(clone) == clone.resolve()

    def test_somewhere_git_cannot_answer_about_falls_back_to_where_it_started(
        self, tmp_path: Path
    ) -> None:
        """Never an exception: a gate stage that crashes says less than one that reports."""
        outside = tmp_path / "not-a-repo"
        outside.mkdir()

        assert export_roadmap.repository_root(outside) == outside


class TestCannotVerify:
    """ "No store here" is a different answer from "the file is wrong", and says so."""

    def test_an_unregistered_checkout_passes_the_check_and_explains(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Somebody who clones the public repository has no database to compare against.

        Failing them would assert something the machine cannot know, and would make the
        gate impossible to pass outside this one workstation.
        """
        monkeypatch.setattr(export_roadmap, "resolve_project", lambda *a, **k: None)

        assert export_roadmap.main([str(tmp_path / "ROADMAP.md"), "--check"]) == 0
        assert "cannot be verified" in capsys.readouterr().out

    def test_but_writing_without_a_store_fails_rather_than_writing_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit request to generate has no honest silent outcome."""
        monkeypatch.setattr(export_roadmap, "resolve_project", lambda *a, **k: None)
        target = tmp_path / "ROADMAP.md"

        assert export_roadmap.main([str(target)]) == 1
        assert not target.exists()

    def test_a_registered_project_whose_database_does_not_exist_yet(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = type("P", (), {"id": "somewhere"})()
        monkeypatch.setattr(export_roadmap, "resolve_project", lambda *a, **k: project)
        monkeypatch.setattr(export_roadmap, "database_for", lambda *a, **k: tmp_path / "none.db")

        assert export_roadmap.main([str(tmp_path / "ROADMAP.md"), "--check"]) == 0
        assert "has no database" in capsys.readouterr().out


class TestStaleness:
    """The contract ``openapi.json`` is held to, over the working tree rather than HEAD."""

    @pytest.fixture(autouse=True)
    def _a_store_that_renders_one_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        project = type("P", (), {"id": "somewhere"})()
        database = tmp_path / "store.db"
        database.write_bytes(b"")
        monkeypatch.setattr(export_roadmap, "resolve_project", lambda *a, **k: project)
        monkeypatch.setattr(export_roadmap, "database_for", lambda *a, **k: database)
        monkeypatch.setattr(export_roadmap, "roadmap_for", lambda *a, **k: "the roadmap\n")

    def test_a_matching_file_passes(self, tmp_path: Path) -> None:
        target = tmp_path / "ROADMAP.md"
        target.write_text("the roadmap\n", encoding="utf-8")

        assert export_roadmap.main([str(target), "--check"]) == 0

    def test_a_hand_edited_file_fails_and_names_the_command_that_fixes_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An edit is what the stage exists to catch, and the message has to be actionable.

        The repair is never "edit it back": it is one command, and printing it is what
        stops a red stage becoming a puzzle.
        """
        target = tmp_path / "ROADMAP.md"
        target.write_text("the roadmap, but somebody tidied it\n", encoding="utf-8")

        assert export_roadmap.main([str(target), "--check"]) == 1
        out = capsys.readouterr().out
        assert "is stale" in out
        assert "export_roadmap.py ROADMAP.md" in out

    def test_a_missing_file_is_stale_rather_than_an_error(self, tmp_path: Path) -> None:
        assert export_roadmap.main([str(tmp_path / "absent.md"), "--check"]) == 1

    def test_writing_makes_the_check_pass(self, tmp_path: Path) -> None:
        """The loop the failure message promises actually closes."""
        target = tmp_path / "ROADMAP.md"

        assert export_roadmap.main([str(target)]) == 0
        assert export_roadmap.main([str(target), "--check"]) == 0

    def test_it_is_written_with_unix_newlines_on_every_platform(self, tmp_path: Path) -> None:
        """Otherwise the file this machine writes is stale the moment Linux reads it."""
        target = tmp_path / "ROADMAP.md"

        export_roadmap.main([str(target)])

        assert b"\r\n" not in target.read_bytes()


class TestLeakRefusal:
    def test_a_leaking_record_fails_the_check_and_names_the_record(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate goes red rather than the file going out, and says which record to fix."""
        from agentjobs.roadmap import Leak, RoadmapLeakError

        project = type("P", (), {"id": "somewhere"})()
        database = tmp_path / "store.db"
        database.write_bytes(b"")
        monkeypatch.setattr(export_roadmap, "resolve_project", lambda *a, **k: project)
        monkeypatch.setattr(export_roadmap, "database_for", lambda *a, **k: database)

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise RoadmapLeakError(
                [Leak(task_id="task-007", field="title", what="an email address", matched="a@b.co")]
            )

        monkeypatch.setattr(export_roadmap, "roadmap_for", _raise)
        target = tmp_path / "ROADMAP.md"

        assert export_roadmap.main([str(target), "--check"]) == 1
        out = capsys.readouterr().out
        assert "task-007 title contains an email address" in out
        assert "Fix the task record" in out
        assert not target.exists()
