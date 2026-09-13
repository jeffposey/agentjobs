"""A finish whose process died is resumed by the poller, once, and then parked (task-443).

**Real repositories, real merges, and real dead processes.** A kill is simulated the way
``test_finish_durable`` simulates one -- ``KeyboardInterrupt`` at the line under test --
and then made to look like what a kill actually leaves: the attempt's recorded pid is a
process that has exited, and the task's run lock is still on disk naming it. Nothing
here tells the resume that the attempt is dead; ``finish_status`` has to conclude it.

The spawn is the one seam. The production spawn starts a detached process; here the same
arguments run ``finish_task`` in this process, so "the gate was not run again" is still a
count of real gate invocations, and a resumed attempt that dies again is a real attempt
with a real directory.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs.dispatch import finish as finish_module
from agentjobs.dispatch.finish import (
    DECLINED,
    FINISHED,
    POSTURE,
    FinishDirectory,
    FinishResult,
    finish_task,
    spawn_finish,
)
from agentjobs.dispatch.finish_resume import (
    PARK_CLAIM,
    PARKED,
    RESUME_CLAIM,
    RESUMED,
    SKIPPED,
    WAITING,
    newest_attempts,
    resume_finish_of_settled_run,
    resume_interrupted_finishes,
)
from agentjobs.dispatch.finish_status import newest_finish_directory, read_meta
from agentjobs.dispatch.ledger import run_lock_path
from agentjobs.dispatch.phases import RUN_ID_ENV
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import runs_root
from agentjobs.models_v2 import Ball, BallReason, Lifecycle
from agentjobs.projects import ProjectRegistry
import test_dispatch_finish
from test_approval_standdown import approve
from test_dispatch_finish import DISPATCHABLE_CONFIG, git, landed, merged_into, settings
from test_finish_durable import gate_calls, install_gate, merges_on_main


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_dispatch_finish``'s real clone, branch, worktree and task, on a finishing machine."""
    built: Dict[str, Any] = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    # The actor vocabulary is what makes "Jeff Posey" a human, and so an approval by him
    # one that stands. Ignored by git exactly as `make_dispatchable` ignores it.
    root: Path = built["root"]
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(DISPATCHABLE_CONFIG), encoding="utf-8"
    )
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    git(root, "add", "--", ".gitignore")
    git(root, "commit", "-m", "chore: ignore the machine-local config")
    # Whether this machine offers the finish is dispatch.yaml's question, and not this
    # suite's: every test here is about what happens on a machine that does.
    monkeypatch.setattr(
        "agentjobs.dispatch.finish_resume.finish_is_offered", lambda *args, **kwargs: True
    )
    return built


class InProcessSpawn:
    """``spawn_finish``'s arguments, run as ``finish_task`` here. A death stays a death."""

    def __init__(self, world: Dict[str, Any], *, starts: bool = True) -> None:
        self.world = world
        self.starts = starts
        self.calls: List[Dict[str, Any]] = []
        self.results: List[Optional[FinishResult]] = []

    def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        if not self.starts:
            return "started, and then never began"
        try:
            result = finish_task(
                manager=self.world["manager"],
                project=kwargs["project"],
                task_id=kwargs["task_id"],
                approver=kwargs["approver"],
                home=kwargs["home"],
                api_base="http://127.0.0.1:1",
                settings=settings(),
                resumed_from=kwargs["resumed_from"],
            )
        except KeyboardInterrupt:
            result = None  # the resumed process died too
        self.results.append(result)
        return "spawned"


class Recorder:
    """A spawn that only writes down what it was asked to start."""

    def __init__(self, calls: List[Dict[str, Any]]) -> None:
        self.calls = calls

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "spawned"


def dead_pid() -> int:
    """The pid of a process that has exited, which is what a killed finish leaves."""
    completed = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(completed.stdout.strip())


def killed(world: Dict[str, Any]) -> Path:
    """Make the newest attempt look like what a kill leaves: a dead pid, holding the lock."""
    directory = newest_finish_directory(world["home"], world["task_id"], "demo")
    assert directory is not None
    pid = dead_pid()
    FinishDirectory(path=directory, finish_id=directory.name).write_meta(pid=pid)
    lock = run_lock_path(world["home"], world["task_id"], project_id="demo")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"pid={pid} run= kind=finish finish={directory.name}", encoding="ascii")
    return directory


REAL = {name: getattr(finish_module, name) for name in ("gate_the_branch", "mark_branch_merged")}
"""The steps a test kills in, kept so the kill can be lifted without ``monkeypatch.undo``.

``undo`` would also lift the fixture's own patches -- the worktree interpreter among them.
"""


def die_in(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def die(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt(f"the process was killed in {name}")

    monkeypatch.setattr(finish_module, name, die)


def crash(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, step: str) -> Path:
    die_in(monkeypatch, step)
    with pytest.raises(KeyboardInterrupt):
        finish_task(
            manager=world["manager"],
            project=world["project"],
            task_id=world["task_id"],
            approver="Jeff Posey",
            home=world["home"],
            api_base="http://127.0.0.1:1",
            settings=settings(),
        )
    return killed(world)


def tick(world: Dict[str, Any], spawn: Any) -> List[Any]:
    return resume_interrupted_finishes(
        world["home"],
        registry=ProjectRegistry(world["home"]),
        managers={"demo": world["manager"]},
        spawn=spawn,
    )


def the_task(world: Dict[str, Any]) -> Any:
    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    return task


# ----- a1: killed after the merge ------------------------------------------------------


class TestKilledAfterTheMerge:
    def test_the_poller_resumes_it_and_it_delivers_without_gating_again(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_gate(world, "green")
        approve(world)
        interrupted = crash(world, monkeypatch, "mark_branch_merged")
        monkeypatch.setattr(finish_module, "mark_branch_merged", REAL["mark_branch_merged"])
        merged = merges_on_main(world["root"])
        assert len(merged) == 1
        gated = len(gate_calls(calls))
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.WORK)

        spawn = InProcessSpawn(world)
        decisions = tick(world, spawn)

        assert [(item.finish_id, item.action) for item in decisions] == [
            (interrupted.name, RESUMED)
        ]
        assert len(spawn.calls) == 1
        assert spawn.calls[0]["resumed_from"] == interrupted.name
        assert spawn.calls[0]["posture_run_id"] == ""
        result = spawn.results[0]
        assert result is not None, "the resumed attempt died"
        assert result.outcome == FINISHED, result.render()
        assert merges_on_main(world["root"]) == merged
        assert len(gate_calls(calls)) == gated, "the resume gated a merged branch again"
        assert the_task(world).lifecycle is Lifecycle.CLOSED
        resumed = newest_finish_directory(world["home"], world["task_id"], "demo")
        assert resumed is not None and read_meta(resumed)["resumed_from"] == interrupted.name
        assert (interrupted / RESUME_CLAIM).is_file()

        assert tick(world, spawn) == []
        assert len(spawn.calls) == 1

    def test_the_live_poll_tick_is_what_starts_it(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        interrupted = crash(world, monkeypatch, "mark_branch_merged")
        started: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agentjobs.dispatch.finish_resume.spawn_finish",
            Recorder(started),
        )

        results = poll_live_sessions(
            world["home"],
            registry=ProjectRegistry(world["home"]),
            managers={"demo": world["manager"]},
        )

        assert [r.detail for r in results if r.run_id == interrupted.name] == [
            f"{world['task_id']}: resumed (on the standing approval)"
        ]
        assert [call["resumed_from"] for call in started] == [interrupted.name]


# ----- a2: killed before the merge, once and then twice --------------------------------


class TestKilledBeforeTheMerge:
    def test_it_is_re_run_once_and_merges(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        crash(world, monkeypatch, "gate_the_branch")
        monkeypatch.setattr(finish_module, "gate_the_branch", REAL["gate_the_branch"])
        assert not merged_into(world["root"], world["branch"])

        spawn = InProcessSpawn(world)
        decisions = tick(world, spawn)

        assert [item.action for item in decisions] == [RESUMED]
        result = spawn.results[0]
        assert result is not None, "the resumed attempt died"
        assert result.outcome == FINISHED, result.render()
        assert landed(world["root"], result)

    def test_a_second_interruption_parks_the_task_for_a_human(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        crash(world, monkeypatch, "gate_the_branch")  # still dies: the resume dies too

        spawn = InProcessSpawn(world)
        assert [item.action for item in tick(world, spawn)] == [RESUMED]
        assert spawn.results == [None]
        second = killed(world)
        assert read_meta(second)["resumed_from"]

        decisions = tick(world, spawn)

        assert [(item.finish_id, item.action, item.reason) for item in decisions] == [
            (second.name, PARKED, "interrupted_again")
        ]
        assert len(spawn.calls) == 1, "a resumed attempt that died was resumed again"
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert task.ball_prompt.startswith("**Nothing was merged.**")
        assert f"agentjobs finish {world['task_id']} --project demo" in task.ball_prompt
        assert (second / PARK_CLAIM).is_file()
        entries = len(task.log)

        assert tick(world, spawn) == []
        assert len(the_task(world).log) == entries, "the park was written twice"

    def test_a_resume_that_never_began_parks(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        interrupted = crash(world, monkeypatch, "gate_the_branch")
        spawn = InProcessSpawn(world, starts=False)
        assert [item.action for item in tick(world, spawn)] == [RESUMED]

        decisions = tick(world, spawn)

        assert [(item.finish_id, item.reason) for item in decisions] == [
            (interrupted.name, "resume_never_started")
        ]
        assert the_task(world).ball is Ball.HUMAN

    def test_a_resume_that_declined_parks(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        interrupted = crash(world, monkeypatch, "gate_the_branch")
        declined = FinishDirectory.create(
            world["home"],
            world["task_id"],
            "demo",
            authority="approval",
            resumed_from=interrupted.name,
        )
        declined.write_meta(outcome=DECLINED, reason="dirty_worktree", finished_at="later")

        decisions = tick(world, InProcessSpawn(world))

        assert [(item.finish_id, item.reason) for item in decisions] == [
            (declined.finish_id, "resume_declined")
        ]
        assert the_task(world).ball is Ball.HUMAN


# ----- a3: a withdrawn approval, or a Stop ---------------------------------------------


def a_stop_now(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return [
        {
            "run_id": "run_x",
            "requester": "Jeff Posey",
            "source": "gui",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


class TestAuthorityIsReadBeforeResuming:
    def test_an_approval_replaced_by_request_changes_is_not_resumed(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        interrupted = crash(world, monkeypatch, "gate_the_branch")
        world["manager"].handoff(
            world["task_id"],
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Changes, please.",
        )
        spawn = InProcessSpawn(world)

        decisions = tick(world, spawn)

        assert [(item.action, item.reason) for item in decisions] == [
            (SKIPPED, "approval_withdrawn")
        ]
        assert spawn.calls == []
        assert (the_task(world).ball, the_task(world).ball_reason) == (
            Ball.AGENT,
            BallReason.REVISE,
        )
        assert not (interrupted / RESUME_CLAIM).exists()
        assert tick(world, spawn) == [], "a skip is decided once, not every ten seconds"

    def test_a_stop_since_the_attempt_began_is_not_resumed(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        crash(world, monkeypatch, "gate_the_branch")
        monkeypatch.setattr("agentjobs.dispatch.approval.stop_requests", a_stop_now)
        spawn = InProcessSpawn(world)

        decisions = tick(world, spawn)

        assert [item.action for item in decisions] == [SKIPPED]
        assert spawn.calls == []

    def test_a_process_still_running_is_waited_on_not_resumed(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        approve(world)
        interrupted = crash(world, monkeypatch, "gate_the_branch")
        FinishDirectory(path=interrupted, finish_id=interrupted.name).write_meta(pid=_own_pid())
        spawn = InProcessSpawn(world)

        decisions = tick(world, spawn)

        assert [item.action for item in decisions] == [WAITING]
        assert spawn.calls == []
        assert not (interrupted / RESUME_CLAIM).exists()

    def test_an_attempt_that_recorded_no_authority_is_left_alone(
        self, world: Dict[str, Any]
    ) -> None:
        approve(world)
        attempt = FinishDirectory.create(world["home"], world["task_id"], "demo")
        attempt.write_meta(pid=dead_pid())

        assert newest_attempts(world["home"])
        assert tick(world, InProcessSpawn(world)) == []


def _own_pid() -> int:
    import os

    return os.getpid()


# ----- a posture finish whose run is gone ----------------------------------------------


def a_run(world: Dict[str, Any], run_id: str, **fields: Any) -> None:
    directory = runs_root(world["home"]) / run_id
    directory.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "task_id": world["task_id"],
        "project_id": "demo",
        "mode": "session",
        "posture": "autonomous",
        "status": "finished",
        "outcome": "failed",
        **fields,
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")


def a_posture_attempt(world: Dict[str, Any], run_id: str) -> FinishDirectory:
    attempt = FinishDirectory.create(
        world["home"], world["task_id"], "demo", authority=POSTURE, run_id=run_id
    )
    attempt.write_meta(pid=dead_pid())
    return attempt


class TestAPostureFinishWhoseRunIsGone:
    def test_it_is_resumed_on_the_run_that_was_granted_the_posture(
        self, world: Dict[str, Any]
    ) -> None:
        a_run(world, "run_gone")
        attempt = a_posture_attempt(world, "run_gone")
        started: List[Dict[str, Any]] = []

        decisions = tick(world, Recorder(started))

        assert [(item.finish_id, item.action) for item in decisions] == [
            (attempt.finish_id, RESUMED)
        ]
        assert started[0]["posture_run_id"] == "run_gone"
        assert started[0]["approver"] == "run run_gone"

    def test_a_cancelled_run_is_a_stop_and_is_not_resumed(self, world: Dict[str, Any]) -> None:
        a_run(world, "run_stopped", status="cancelled", outcome="cancelled")
        a_posture_attempt(world, "run_stopped")
        started: List[Dict[str, Any]] = []

        decisions = tick(world, Recorder(started))

        assert [(item.action, item.reason) for item in decisions] == [(SKIPPED, "stopped")]
        assert started == []

    def test_the_settle_path_resumes_only_its_own_runs_finish(self, world: Dict[str, Any]) -> None:
        a_run(world, "run_gone")
        a_posture_attempt(world, "run_gone")
        started: List[Dict[str, Any]] = []

        def settle(run_id: str) -> bool:
            return resume_finish_of_settled_run(
                world["home"],
                project_id="demo",
                task_id=world["task_id"],
                run_id=run_id,
                manager=world["manager"],
                spawn=Recorder(started),
            )

        assert not settle("run_other")
        assert started == []
        assert settle("run_gone")
        assert len(started) == 1


# ----- the spawn and the command it runs -----------------------------------------------


class TestTheSpawn:
    def test_a_posture_resume_carries_the_run_and_the_attempt(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: Dict[str, Any] = {}

        def popen(argv: List[str], **kwargs: Any) -> None:
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")

        monkeypatch.setattr(finish_module.subprocess, "Popen", popen)

        spawn_finish(
            project=world["project"],
            task_id=world["task_id"],
            approver="run run_gone",
            home=world["home"],
            resumed_from="fin_dead",
            posture_run_id="run_gone",
        )

        argv = seen["argv"]
        assert argv[argv.index("--resumed-from") + 1] == "fin_dead"
        assert "--posture-release" in argv
        assert seen["env"][RUN_ID_ENV] == "run_gone"

    def test_an_approval_spawn_is_unchanged(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: Dict[str, Any] = {}

        def popen(argv: List[str], **kwargs: Any) -> None:
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")

        monkeypatch.setattr(finish_module.subprocess, "Popen", popen)

        spawn_finish(
            project=world["project"], task_id=world["task_id"], approver="Jeff", home=world["home"]
        )

        assert "--resumed-from" not in seen["argv"]
        assert "--posture-release" not in seen["argv"]
        assert seen["env"] is None

    def test_the_command_passes_the_attempt_it_resumes(
        self, world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.cli import app

        received: Dict[str, Any] = {}

        def fake_finish(**kwargs: Any) -> FinishResult:
            received.update(kwargs)
            return FinishResult(task_id=kwargs["task_id"], outcome=DECLINED, reason="x", detail="y")

        monkeypatch.setattr(finish_module, "finish_task", fake_finish)

        outcome = CliRunner().invoke(
            app, ["finish", world["task_id"], "--project", "demo", "--resumed-from", "fin_dead"]
        )

        assert outcome.exit_code == 2, outcome.output
        assert received["resumed_from"] == "fin_dead"

    def test_an_attempt_records_what_authorised_it(self, world: Dict[str, Any]) -> None:
        result = finish_task(
            manager=world["manager"],
            project=world["project"],
            task_id=world["task_id"],
            approver="Jeff Posey",
            home=world["home"],
            api_base="http://127.0.0.1:1",
            settings=settings(),
        )
        assert result.directory is not None
        meta = read_meta(result.directory)
        assert meta["authority"] == "approval"
        assert isinstance(meta["pid"], int)
        assert "run_id" not in meta and "resumed_from" not in meta
