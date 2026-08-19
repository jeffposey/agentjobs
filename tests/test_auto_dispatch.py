"""Auto-dispatch: the cap boundaries, and the rule the safety argument rests on.

The rule is that an agent's handoff can never cause a dispatch. Everything else here is
a backstop. It is tested first and tested directly, because a structural guarantee
nobody checks is a comment.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.auto import check_budget, last_dispatch_at, maybe_auto_dispatch
from agentjobs.dispatch.config import AutoDispatchLimits
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchPosture,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
    utcnow,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

CONFIG = {
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
    (root / ".gitignore").write_text("tasks/\n.agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)

    ProjectRegistry(home=home).add(root, project_id="sandbox")

    with TestClient(app) as client:
        yield client, root, home

    reset_dependency_cache()


def write_dispatch_config(home: Path, tmp_path: Path, *, auto: bool) -> None:
    """A machine-local config whose runner exits immediately."""
    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {
                        "argv": [sys.executable, str(runner), "{prompt}"],
                        "actor": "claude",
                    }
                },
                "projects": {"sandbox": {"enabled": True, "runner": "fake", "auto_dispatch": auto}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def manager_for(root: Path) -> TaskManager:
    return TaskManager(TaskStorage(root / "tasks"))


def seed_task(root: Path, *, ball: Ball = Ball.HUMAN) -> str:
    """A task waiting on a human, ready to be approved."""
    manager = manager_for(root)
    task = manager.create_task(
        title="Auto-dispatchable",
        category="general",
        summary="A task to auto-dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    if ball is Ball.HUMAN:
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please review.",
        )
    return task.id


def add_dispatch_entry(manager: TaskManager, task_id: str, run_id: str) -> None:
    """A dispatch entry exactly as the runner writes one, so counts are real."""
    manager.record_dispatch(
        task_id,
        actor="Jeff Posey",
        run_id=run_id,
        agent="fake",
        runner="fake",
        mode=DispatchMode.BATCH,
        posture=DispatchPosture.SUPERVISED,
        trigger=DispatchTrigger.AUTO,
        caused_by=1,
        argv=["python", "-c", "pass"],
        cwd=".",
        git_head="abc1234",
    )


def runs_in(home: Path) -> list:
    """Run directories on this machine. `.locks` lives beside them and is not a run."""
    root = home / "runs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != ".locks")


# ----- the rule ---------------------------------------------------------------


class TestTheHumanClockedRule:
    """An agent's handoff never causes a dispatch. This is the whole safety argument."""

    def test_an_agent_handoff_starts_nothing_even_with_auto_dispatch_on(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)

        # An agent hands the ball to another agent: precisely the shape of a runaway
        # loop's second turn.
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
        assert runs_in(home) == []

    def test_the_rule_holds_even_when_every_budget_cap_has_room(
        self, served, tmp_path: Path
    ) -> None:
        """The caps are a backstop. Removing them must not make the loop possible."""
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        task = manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Carry on.",
        )

        assert check_budget(task, AutoDispatchLimits()) is None
        outcome = maybe_auto_dispatch(
            manager=manager,
            project=ProjectRegistry(home=home).get("sandbox"),
            project_config=CONFIG,
            task=task,
        )

        assert outcome.started is False


# ----- the trigger ------------------------------------------------------------


class TestApprovalTriggersARun:
    def test_approving_starts_a_run_with_no_further_interaction(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/approve", json={"user": "Jeff Posey"}
        )

        assert response.status_code == 200, response.text
        assert len(runs_in(home)) == 1
        task = manager_for(root).get_task(task_id)
        assert task is not None
        assert task.dispatch_count == 1

    def test_approving_starts_nothing_when_auto_dispatch_is_off(
        self, served, tmp_path: Path
    ) -> None:
        """Off is the default, and the default is the whole safety posture."""
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=False)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/approve", json={"user": "Jeff Posey"}
        )

        assert response.status_code == 200, response.text
        assert runs_in(home) == []

    def test_a_machine_with_no_dispatch_config_approves_exactly_as_before(self, served) -> None:
        client, root, home = served
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/approve", json={"user": "Jeff Posey"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["task"]["ball"] == "agent"
        assert runs_in(home) == []

    def test_requesting_changes_also_starts_a_run(self, served, tmp_path: Path) -> None:
        """It is a human act that moves the ball to an agent, exactly as approving is."""
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert response.status_code == 200, response.text
        assert len(runs_in(home)) == 1

    def test_rejecting_starts_nothing_because_the_task_is_closed(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/reject",
            json={"user": "Jeff Posey", "reason": "Out of scope."},
        )

        assert response.status_code == 200, response.text
        assert runs_in(home) == []


# ----- the caps ---------------------------------------------------------------


class TestBudgetCaps:
    """Each cap refuses at its boundary and not before."""

    def test_the_lifetime_cap_allows_the_last_permitted_run_and_refuses_the_next(
        self, served
    ) -> None:
        _, root, _ = served
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        limits = AutoDispatchLimits(per_task_lifetime=3, per_task_per_day=99, cooldown_seconds=0)

        for index in range(2):
            add_dispatch_entry(manager, task_id, f"run_{index}")
        task = manager.get_task(task_id)
        assert task is not None and task.dispatch_count == 2
        assert check_budget(task, limits) is None, "two of three is still under the cap"

        add_dispatch_entry(manager, task_id, "run_2")
        task = manager.get_task(task_id)
        assert task is not None
        refusal = check_budget(task, limits)
        assert refusal is not None
        assert refusal.limit == "per_task_lifetime"
        assert "3" in refusal.message
        assert refusal.parks_task is True

    def test_the_daily_cap_counts_only_the_last_twenty_four_hours(self, served) -> None:
        _, root, _ = served
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        limits = AutoDispatchLimits(per_task_per_day=3, per_task_lifetime=99, cooldown_seconds=0)

        for index in range(3):
            add_dispatch_entry(manager, task_id, f"run_{index}")
        task = manager.get_task(task_id)
        assert task is not None

        refusal = check_budget(task, limits)
        assert refusal is not None and refusal.limit == "per_task_per_day"

        # The same three dispatches, judged from a day later, no longer count.
        tomorrow = utcnow() + timedelta(days=1, seconds=1)
        assert check_budget(task, limits, now=tomorrow) is None

    def test_the_cooldown_refuses_inside_the_window_and_relents_outside_it(self, served) -> None:
        _, root, _ = served
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        limits = AutoDispatchLimits(cooldown_seconds=60, per_task_per_day=99, per_task_lifetime=99)

        add_dispatch_entry(manager, task_id, "run_0")
        task = manager.get_task(task_id)
        assert task is not None

        refusal = check_budget(task, limits)
        assert refusal is not None
        assert refusal.limit == "cooldown"
        # Transient: waiting fixes it, so it does not park the task with a human.
        assert refusal.parks_task is False

        assert check_budget(task, limits, now=utcnow() + timedelta(seconds=61)) is None

    def test_a_task_that_has_never_been_dispatched_has_no_last_dispatch(self, served) -> None:
        _, root, _ = served
        task_id = seed_task(root, ball=Ball.AGENT)
        task = manager_for(root).get_task(task_id)
        assert task is not None

        assert last_dispatch_at(task) is None
        assert check_budget(task, AutoDispatchLimits()) is None

    def test_an_exhausted_budget_reports_the_count_cap_rather_than_the_cooldown(
        self, served
    ) -> None:
        """Both apply; being told to wait 60s for a refusal that will not change is worse."""
        _, root, _ = served
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        limits = AutoDispatchLimits(per_task_lifetime=2, per_task_per_day=2, cooldown_seconds=600)

        add_dispatch_entry(manager, task_id, "run_0")
        add_dispatch_entry(manager, task_id, "run_1")
        task = manager.get_task(task_id)
        assert task is not None

        refusal = check_budget(task, limits)
        assert refusal is not None and refusal.limit == "per_task_lifetime"


class TestATrippedCapIsNeverSilent:
    def test_it_names_the_limit_on_the_record_and_parks_the_task(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        # The default lifetime cap is 10; spend it.
        for index in range(10):
            add_dispatch_entry(manager, task_id, f"run_{index}")
        task = manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Have another go.",
        )

        outcome = maybe_auto_dispatch(
            manager=manager,
            project=ProjectRegistry(home=home).get("sandbox"),
            project_config=CONFIG,
            task=task,
        )

        assert outcome.started is False
        assert outcome.reason == "per_task_lifetime"

        after = manager.get_task(task_id)
        assert after is not None
        notes = [entry for entry in after.log if entry.type is LogEntryType.NOTE]
        assert any("per_task_lifetime" in (entry.body or "") for entry in notes)
        assert any(
            entry.data.get("auto_dispatch_refused") == "per_task_lifetime" for entry in notes
        )
        # Parked with a person, because a task burning its budget is reporting a
        # problem with itself and nobody will look unless it asks them to.
        assert after.ball is Ball.HUMAN
        assert after.ball_reason is BallReason.DECISION
        assert "manual dispatch still works" in (after.ball_prompt or "").lower()
        assert runs_in(home) == []

    def test_a_cooldown_refusal_is_logged_but_does_not_park_the_task(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        add_dispatch_entry(manager, task_id, "run_0")
        task = manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Have another go.",
        )

        outcome = maybe_auto_dispatch(
            manager=manager,
            project=ProjectRegistry(home=home).get("sandbox"),
            project_config=CONFIG,
            task=task,
        )

        assert outcome.reason == "cooldown"
        after = manager.get_task(task_id)
        assert after is not None
        assert any("cooldown" in (entry.body or "") for entry in after.log)
        assert after.ball is Ball.AGENT, "waiting fixes a cooldown; it is not a decision"


class TestManualDispatchIsNotCapped:
    def test_a_task_over_every_cap_can_still_be_dispatched_by_hand(
        self, served, tmp_path: Path
    ) -> None:
        """D3: a human clicking Dispatch repeatedly is a decision, not a malfunction."""
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        manager = manager_for(root)
        task_id = seed_task(root, ball=Ball.AGENT)
        for index in range(20):
            add_dispatch_entry(manager, task_id, f"run_{index}")
        manager.add_log_entry(
            task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go anyway."
        )

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 202, response.text
        assert len(runs_in(home)) == 1
