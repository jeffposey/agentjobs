"""Getting a human's handback to the agent it is addressed to (task-384).

The occurrence: Request Changes moved the ball to ``agent``/``revise``, correctly and on
the record, and then nothing happened and nothing said why. The session that asked for
the review was still alive, so ``dispatch_task`` refused with ``live_run_exists``,
``maybe_auto_dispatch`` turned that into a returned outcome, and the route dropped it on
the floor. The dashboard read *Revising (claude)* beside a live run for the better part
of an hour.

So there are two things to hold here, and they are separate tests because they are
separate failures:

- **Nothing is silent.** Every outcome of a human handback writes exactly one log entry.
  A path that writes none is the bug; a path that writes two is the bug's mirror image
  and is just as wrong.
- **Nothing starts a rival.** A task with a live run gets no second one, ever. The
  feedback reaches the session that is already there, once that session is done.

The whole live-run half is driven through the run *ledger* -- a directory with a
``meta.yaml`` in it -- rather than by starting a real agent, because what
``dispatch_task`` and ``deliver_handback`` actually read is that directory. A fake run
whose meta says ``running`` is not a stand-in for a live run here; it is what a live run
is, to every line of code under test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.handback import pending_handback
from agentjobs.dispatch.ledger import HEALTH_HANDBACK, list_runs, run_health
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchMode,
    DispatchPosture,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

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


def write_dispatch_config(home: Path, tmp_path: Path, *, auto: bool = True) -> None:
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


def seed_task(root: Path) -> str:
    """A task an agent has handed back for review, which is where every handback starts."""
    manager = manager_for(root)
    task = manager.create_task(
        title="Handbackable",
        category="general",
        summary="A task to hand back.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.handoff(
        task.id,
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Please review.",
    )
    return task.id


def fake_live_run(home: Path, task_id: str, *, run_id: str = "run_live", mode: str = "batch") -> Path:
    """A run directory that is live, as far as every reader in the system is concerned.

    ``batch`` by default and not ``session``: a session run is one the handback path will
    try to *poll*, which shells out to a session manager that is not here. Batch exercises
    the same live-run refusal without a subprocess, and the session-specific behaviour is
    tested where the runner is faked instead.
    """
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    (directory / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": run_id,
                "task_id": task_id,
                "project_id": "sandbox",
                "mode": mode,
                "status": "running",
                "started_at": "2026-09-07T19:32:54+00:00",
                "dispatch_entry_id": 1,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return directory


def spend_one_dispatch(root: Path, task_id: str) -> None:
    """A dispatch entry exactly as the runner writes one, so the caps count it."""
    manager_for(root).record_dispatch(
        task_id,
        actor="Jeff Posey",
        run_id="run_spent",
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


def runs_in(home: Path) -> List[str]:
    root = home / "runs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != ".locks")


def dispatcher_notes(task: Task) -> List[str]:
    """Every note AgentJobs itself wrote. The thing that was missing entirely."""
    return [
        entry.body
        for entry in task.log
        if entry.type is LogEntryType.NOTE and entry.actor == "dispatcher"
    ]


def reload(root: Path, task_id: str) -> Task:
    task = manager_for(root).get_task(task_id)
    assert task is not None
    return task


HANDBACKS = [
    ("request-changes", {"feedback": "Rename the thing."}),
    ("redirect", {"feedback": "Different approach, please."}),
    ("answer", {"feedback": "Yes, use the second one."}),
]
"""The verbs that hand the ball back to an agent, as the review panel posts them.

All three route through ``after_human_handoff`` and all three failed the same way, which
is ac-5: Request Changes was the observed case and the others were never separately
broken or separately fixed. Parameterised rather than written three times so a fourth
verb added later fails here rather than silently joining them.
"""


# ----- nothing is silent ------------------------------------------------------


class TestEveryOutcomeWritesExactlyOneEntry:
    """ac-3. A refusal that records nothing is indistinguishable from a success."""

    @pytest.mark.parametrize("verb,payload", HANDBACKS)
    def test_a_live_run_blocks_the_handback_and_says_so(
        self, served, tmp_path: Path, verb: str, payload: dict
    ) -> None:
        """The observed failure, from the click down. This is the regression test."""
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)
        fake_live_run(home, task_id)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/{verb}",
            json={"user": "Jeff Posey", **payload},
        )

        assert response.status_code == 200, response.text
        notes = dispatcher_notes(reload(root, task_id))
        assert len(notes) == 1, notes
        assert "run_live" in notes[0]
        assert "one live run per task" in notes[0]

    def test_a_delivered_handback_writes_its_dispatch_entry_and_no_note(
        self, served, tmp_path: Path
    ) -> None:
        """The other half of *exactly one*: a success must not also narrate itself."""
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert response.status_code == 200, response.text
        task = reload(root, task_id)
        assert task.dispatch_count == 1
        assert dispatcher_notes(task) == []

    def test_a_machine_that_does_not_dispatch_stays_quiet(self, served) -> None:
        """Configuration is not an event.

        Without this, every human action on every project that has never opted into
        dispatch would grow a note saying so -- which is the same failure as silence
        reached from the other end, because a log nobody skims is a log nobody reads.
        """
        client, root, home = served
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert response.status_code == 200, response.text
        assert dispatcher_notes(reload(root, task_id)) == []

    def test_auto_dispatch_switched_off_stays_quiet_too(self, served, tmp_path: Path) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=False)
        task_id = seed_task(root)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert response.status_code == 200, response.text
        assert dispatcher_notes(reload(root, task_id)) == []

    def test_a_tripped_budget_cap_still_writes_one_entry_and_not_two(
        self, served, tmp_path: Path
    ) -> None:
        """``record_cap_refusal`` already speaks, so the handback must not speak again.

        This is what ``AutoDispatchOutcome.recorded`` is for. Before it, the obvious
        implementation of "always write an outcome" would have doubled every cap refusal.
        """
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        (home / "dispatch.yaml").write_text(
            (home / "dispatch.yaml").read_text(encoding="utf-8")
            + yaml.safe_dump({"limits": {"auto": {"per_task_lifetime": 1}}}, sort_keys=False),
            encoding="utf-8",
        )
        task_id = seed_task(root)
        spend_one_dispatch(root, task_id)

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert response.status_code == 200, response.text
        notes = dispatcher_notes(reload(root, task_id))
        assert len(notes) == 1, notes
        assert "budget cap" in notes[0]
        assert runs_in(home) == []


# ----- nothing starts a rival -------------------------------------------------


class TestNoSecondRun:
    """ac-4. A second run would put two agents on one branch with one task record."""

    @pytest.mark.parametrize("verb,payload", HANDBACKS)
    def test_a_task_with_a_live_run_gets_no_second_one(
        self, served, tmp_path: Path, verb: str, payload: dict
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)
        fake_live_run(home, task_id)

        client.post(
            f"/api/projects/sandbox/tasks/{task_id}/{verb}",
            json={"user": "Jeff Posey", **payload},
        )

        assert runs_in(home) == ["run_live"]
        assert reload(root, task_id).dispatch_count == 0

    def test_an_interactive_session_holding_the_task_is_not_displaced(
        self, served, tmp_path: Path
    ) -> None:
        """A person's own chat window is the one holder a handback must never stop."""
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)
        fake_live_run(home, task_id, run_id="run_mine", mode="interactive")

        client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        assert runs_in(home) == ["run_mine"]
        record = next(r for r in list_runs(home) if r.run_id == "run_mine")
        assert record.is_live


# ----- the surface a person reads ---------------------------------------------


class TestABlockedHandbackIsVisible:
    """ac-6. *Revising* beside a live run reads as work happening. It was not."""

    def test_the_run_reports_feedback_waiting_rather_than_working(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)
        fake_live_run(home, task_id)
        assert run_health(next(r for r in list_runs(home) if r.run_id == "run_live")) == "working"

        client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        record = next(r for r in list_runs(home) if r.run_id == "run_live")
        assert record.handback_pending is not None
        assert run_health(record) == HEALTH_HANDBACK

    def test_the_pending_entry_id_is_the_human_handoff_the_agent_still_owes(
        self, served, tmp_path: Path
    ) -> None:
        """Not a boolean: the id is what lets the poller dispatch on the *human's* entry.

        By the time the poller delivers, AgentJobs' own ``dispatch_result`` is the newest
        entry on the task, and a dispatch attributed to that is refused as an agent's --
        correctly. Naming this entry is how the delivery satisfies ``assert_human_clocked``
        rather than working around it.
        """
        client, root, home = served
        write_dispatch_config(home, tmp_path)
        task_id = seed_task(root)
        fake_live_run(home, task_id)

        client.post(
            f"/api/projects/sandbox/tasks/{task_id}/request-changes",
            json={"user": "Jeff Posey", "feedback": "Rename the thing."},
        )

        task = reload(root, task_id)
        record = next(r for r in list_runs(home) if r.run_id == "run_live")
        entry = next(e for e in task.log if e.id == record.handback_pending)
        assert entry.actor == "Jeff Posey"
        assert entry.type is LogEntryType.HANDOFF


# ----- what counts as a handback ----------------------------------------------


class TestPendingHandback:
    """The predicate the poller asks on every settling run, read off the record."""

    def test_a_human_handoff_to_the_agent_is_pending(self, served) -> None:
        _, root, _ = served
        task_id = seed_task(root)
        manager = manager_for(root)
        manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Rename the thing.",
        )

        entry = pending_handback(reload(root, task_id), CONFIG)

        assert entry is not None
        assert entry.actor == "Jeff Posey"

    def test_an_agents_own_handoff_is_not_a_handback(self, served) -> None:
        """The human-clocked rule, asked one step earlier.

        An agent handing work to itself is how a dispatch loop starts, and this predicate
        is now one of the things that could open one. It refuses for the same reason
        ``assert_human_clocked`` does, rather than relying on that check to catch it later.
        """
        _, root, _ = served
        task_id = seed_task(root)
        manager = manager_for(root)
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Carry on.",
        )

        assert pending_handback(reload(root, task_id), CONFIG) is None

    def test_a_hold_is_not_a_handback(self, served) -> None:
        """A hold is the human saying stop; waking on it would answer *stop* with *go*."""
        _, root, _ = served
        task_id = seed_task(root)
        manager = manager_for(root)
        manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.HOLD,
            ball_prompt="Stop until I say.",
        )

        assert pending_handback(reload(root, task_id), CONFIG) is None

    def test_a_ball_still_with_the_human_is_not_a_handback(self, served) -> None:
        _, root, _ = served
        task_id = seed_task(root)

        assert pending_handback(reload(root, task_id), CONFIG) is None

    def test_after_entry_excludes_the_handoff_a_run_was_already_dispatched_for(
        self, served
    ) -> None:
        """The poller's guard. Without it, every settling run dispatches its task again.

        A run's own dispatch answers a handoff that came *before* it. Only a handoff
        written after that is feedback the run has not been given.
        """
        _, root, _ = served
        task_id = seed_task(root)
        manager = manager_for(root)
        manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Rename the thing.",
        )
        task = reload(root, task_id)
        newest = task.log[-1].id

        assert pending_handback(task, CONFIG, after_entry=newest - 1) is not None
        assert pending_handback(task, CONFIG, after_entry=newest) is None
