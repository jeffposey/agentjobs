"""What a process says it is running, and why it must not recompute the answer.

``source_root`` already told a client which *directory* a process imported. That catches
a wrongly-installed server and misses a stale one: a process started before a merge
imports from exactly the right directory and runs exactly the wrong code.

``source_commit`` closes that, and the whole value of it is the one property tested
hardest below -- **it is captured once, at startup, and never recomputed**. A running
server whose clone has since been merged into would otherwise read the new HEAD off disk
and report the merge commit while executing the code it loaded an hour ago. The scripted
finish (task-241) verifies a delivery by asking this question, so the wrong answer would
be a merge reported as live that nobody can see.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentjobs import environment
from agentjobs.api.main import app


@pytest.fixture(autouse=True)
def unfixed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with nothing captured, since it is process-global by design."""
    monkeypatch.setattr(environment, "_IDENTITY", None)


class TestCapture:
    def test_it_is_captured_once_and_then_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commits = iter(["first", "second", "third"])
        monkeypatch.setattr(environment, "_head_commit", lambda root: next(commits))

        first = environment.capture_source_identity()
        assert first.commit == "first"
        # The commit on disk has moved on -- a merge landed. The process has not.
        assert environment.capture_source_identity().commit == "first"
        assert environment.source_identity().commit == "first"

    def test_a_non_checkout_reports_no_commit_rather_than_a_guess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ordinary pip install has no HEAD, and 'cannot be proven' is the honest answer."""
        monkeypatch.setattr(environment, "imported_source_root", lambda: None)
        assert environment.capture_source_identity().commit is None

    def test_it_reads_the_real_head_of_a_real_checkout(self, tmp_path: Path) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
        (root / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
        expected = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert environment._head_commit(root) == expected

    def test_a_directory_that_is_not_a_repository_is_not_an_error(self, tmp_path: Path) -> None:
        assert environment._head_commit(tmp_path) is None

    def test_started_at_is_a_parseable_utc_timestamp(self) -> None:
        stamp = environment.capture_source_identity().started_at
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None


class TestTheVersionEndpoint:
    def test_it_reports_the_captured_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(environment, "_head_commit", lambda root: "a" * 40)
        environment.capture_source_identity()

        payload = TestClient(app).get("/api/version").json()

        assert payload["source_commit"] == "a" * 40
        assert payload["started_at"]
        assert payload["source_root"]

    def test_it_does_not_recompute_when_the_checkout_moves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stale-server case, end to end: HEAD moves and the answer does not."""
        head = ["before-the-merge"]
        monkeypatch.setattr(environment, "_head_commit", lambda root: head[0])
        environment.capture_source_identity()
        client = TestClient(app)
        assert client.get("/api/version").json()["source_commit"] == "before-the-merge"

        head[0] = "the-merge-commit"

        assert client.get("/api/version").json()["source_commit"] == "before-the-merge"

    def test_an_uncapturable_commit_is_null_rather_than_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(environment, "_head_commit", lambda root: None)
        environment.capture_source_identity()
        payload = TestClient(app).get("/api/version").json()
        assert "source_commit" in payload
        assert payload["source_commit"] is None
