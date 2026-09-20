"""Starting an agent on the task you just filed (task-176).

The feature is two browser requests in order -- ``POST /tasks`` then the ordinary
``POST /tasks/{id}/dispatch`` -- and it adds nothing to the server. That is the claim
worth testing, and it has two halves.

**It needs no exemption.** A human filling in a form and pressing create is a human act,
so the entry the create writes satisfies the human-clocked rule on its own terms: the
dispatch that follows is permitted by the gate as it already stood, not by a gate that
was widened to let this surface through.

**The wheel still stops.** The run that starts ends in an agent's handoff, and an
agent's handoff causes nothing. That is the same rule ``test_auto_dispatch.py`` pins for
the approval trigger, checked here on the shape this feature produces: a task created,
dispatched, and handed back within a minute of being thought of.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.auto import maybe_auto_dispatch
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason
from agentjobs.projects import ProjectRegistry
from support import task_store

CONFIG: dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


@pytest.fixture()
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path, Path]]:
    """A served project with a clean git tree, plus a throwaway AgentJobs home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "sandbox"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    (root / "tasks").mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)

    ProjectRegistry(home=home).add(root, project_id="sandbox")

    with TestClient(app) as client:
        yield client, root, home

    reset_dependency_cache()


def enable_dispatch(home: Path, tmp_path: Path, *, auto: bool = False) -> None:
    """A machine-local config whose runner exits immediately."""
    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {"argv": [sys.executable, str(runner), "{prompt}"], "actor": "claude"}
                },
                "projects": {"sandbox": {"enabled": True, "runner": "fake", "auto_dispatch": auto}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def file_task(client: TestClient, *, actor: str | None = "Jeff Posey") -> str:
    """File a task exactly as the create form does: one POST, carrying the creator."""
    body: dict[str, object] = {
        "title": "Filters match nothing",
        "summary": "Every task-list filter returns zero rows.",
        "description": "Make the filters match the rows they name.",
        "lifecycle": "ready",
        "category": "ux",
    }
    if actor is not None:
        body["actor"] = actor
    response = client.post("/api/projects/sandbox/tasks", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def runs_in(home: Path) -> list[str]:
    """Run directories on this machine. `.locks` lives beside them and is not a run."""
    root = home / "runs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != ".locks")


class TestFilingIsItselfTheHumanAct:
    """The create entry authorises the dispatch. No gate is widened to allow that."""

    def test_a_dispatch_straight_after_a_human_create_needs_no_authoriser_at_all(
        self, served, tmp_path: Path
    ) -> None:
        """The plainest statement of the rule: `{}`, resolving the newest entry.

        The newest entry on a just-created task is the create itself, attributed to the
        person who filled in the form. `assert_human_clocked` reads that entry and is
        satisfied -- which is why this surface needs no exemption and gets none. If this
        ever starts failing, the answer is not to add one.
        """
        client, _root, home = served
        enable_dispatch(home, tmp_path)
        task_id = file_task(client)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 202, response.text
        assert response.json()["task_id"] == task_id
        assert len(runs_in(home)) == 1

    def test_the_browser_sends_the_clicker_and_the_guard_writes_their_entry(
        self, served, tmp_path: Path
    ) -> None:
        """What the checkbox actually posts: the ordinary one-click body (task-188)."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = file_task(client)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/dispatch",
            json={"user": "Jeff Posey"},
        )

        assert response.status_code == 202, response.text
        manager = TaskManager(task_store(root / "tasks"))
        task = manager.get_task(task_id)
        assert task is not None
        authorising = task.log[response.json()["caused_by"] - 1]
        assert authorising.actor == "Jeff Posey"

    def test_a_task_filed_with_no_creator_is_refused_rather_than_signed_for(
        self, served, tmp_path: Path
    ) -> None:
        """A create that named nobody leaves an empty log, and an empty log authorises
        nothing. The refusal is the gate working, not a case to special-case."""
        client, _root, home = served
        enable_dispatch(home, tmp_path)
        task_id = file_task(client, actor=None)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code in (403, 409)
        assert response.json()["code"] in {"no_causing_entry", "not_human_clocked"}
        assert runs_in(home) == []


class TestTheWheelStillStops:
    """The run this starts ends in an agent's handoff, and that causes nothing."""

    def test_the_handoff_ending_such_a_run_starts_nothing_further(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        # Auto-dispatch on, which is the only configuration in which anything could
        # follow a handoff at all. The point is that it still does not.
        enable_dispatch(home, tmp_path, auto=True)
        task_id = file_task(client)
        assert (
            client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={}).status_code
            == 202
        )
        started = runs_in(home)
        assert len(started) == 1

        # The agent ends its run the way a runaway's second turn would: handing the ball
        # straight back to an agent. This is the exact shape the human-clocked rule
        # exists to refuse.
        manager = TaskManager(task_store(root / "tasks"))
        task = manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Carry on.",
        )

        outcome = maybe_auto_dispatch(
            manager=manager,
            project=ProjectRegistry(home=home).get("sandbox"),
            project_config=CONFIG,
            task=task,
        )

        assert outcome.started is False
        assert outcome.reason == "not_human_clocked"
        assert runs_in(home) == started

    def test_the_same_handoff_is_refused_over_http_too(self, served, tmp_path: Path) -> None:
        """403 rather than 409: no amount of retrying makes an agent's entry a human's."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = file_task(client)
        assert (
            client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={}).status_code
            == 202
        )
        TaskManager(task_store(root / "tasks")).handoff(
            task_id,
            actor="claude",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Carry on.",
        )

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 403
        assert response.json()["code"] == "not_human_clocked"


class TestARefusedStartKeepsTheTask:
    """Creating and starting are two outcomes, and the first survives the second."""

    def test_a_refused_dispatch_leaves_the_filed_task_exactly_as_it_was(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        task_id = file_task(client)
        before = client.get(f"/api/projects/sandbox/tasks/{task_id}").json()

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "sentinel"
        # The record is untouched: nothing rolls a task back because a run could not
        # start, and nothing writes a dispatch entry for a dispatch that did not happen.
        assert client.get(f"/api/projects/sandbox/tasks/{task_id}").json() == before
        assert runs_in(home) == []
