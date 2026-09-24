"""A finisher never runs on somebody else's lock because of an inherited identity (task-538).

The incident, 2026-09-22: the server at 8876 had been restarted by a run's finish and
carried that run's ``AGENTJOBS_RUN_ID``, ``_DIR`` and ``_CREDENTIAL``. task-536 was
approved while its own review session was still attached. The finish the server spawned
inherited the leaked identity, ``own_run_id`` repaired it to the live session holding
task-536's lock, and the finish took no lock of its own. When the session concluded its
lock was swept as stale, and the merge ran for thirteen minutes with nothing holding the
task -- so every status read judged the live finish dead.

Three layers are pinned here: the approval finish takes (or takes over) its own
``kind=finish`` lock whatever the environment says; ``spawn_finish`` hands the child no
identity it was not granted; and ``agentjobs serve`` drops one it inherited.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

import agentjobs.dispatch.finish as finish_module
from agentjobs.dispatch.credentials import CREDENTIAL_ENV, RUN_IDENTITY_VARS
from agentjobs.dispatch.finish import FINISHED, spawn_finish
from agentjobs.dispatch.finish_status import RUNNING, read_finish_status
from agentjobs.dispatch.ledger import live_runs, read_task_lock_holder, release_stale_locks
from agentjobs.dispatch.phases import RUN_DIR_ENV, RUN_ID_ENV
from agentjobs.models_v2 import Lifecycle
import test_approval_standdown
from test_approval_standdown import (
    approve,
    dispatch_session,
    finish,
    hand_off_for_review,
    result_for,
    the_task,
)
from test_dispatch_finish import landed, make_session_dispatchable


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_approval_standdown``'s world: a real clone, branch, task and skipping clock."""
    built: Dict[str, Any] = test_approval_standdown.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


LEAKED_RUN = "run_ca9c11a7"


def leak_a_finished_runs_identity(
    home: Path, monkeypatch: pytest.MonkeyPatch, *, other_task: str = "task-523"
) -> None:
    """The server's environment on 2026-09-22: a real, finished run against another task."""
    directory = home / "runs" / LEAKED_RUN
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.yaml").write_text(
        yaml.safe_dump({"run_id": LEAKED_RUN, "task_id": other_task, "status": "finished"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(RUN_ID_ENV, LEAKED_RUN)
    monkeypatch.setenv(RUN_DIR_ENV, str(directory))
    monkeypatch.setenv(CREDENTIAL_ENV, f"{LEAKED_RUN}.not-a-real-token")


def test_the_identity_names_are_the_ledgers() -> None:
    """``RUN_IDENTITY_VARS`` spells two names out; this keeps them the ones dispatch sets."""
    assert set(RUN_IDENTITY_VARS) == {RUN_ID_ENV, RUN_DIR_ENV, CREDENTIAL_ENV}


class TestAnApprovalFinishUnderALeakedIdentity:
    def test_it_takes_its_own_lock_and_the_runs_conclusion_leaves_it_in_place(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 2026-09-22 shape, end to end, with the lock read from inside the finish.

        Before task-538 this finish borrowed the live session's lock: the holder read
        mid-merge was ``run=<session>``, ``kind=dispatch``, and the sweep that runs when a
        run concludes deleted it, leaving nothing at all.
        """
        make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approve(world)
        leak_a_finished_runs_identity(world["home"], monkeypatch)
        holder = read_task_lock_holder(world["home"], world["task_id"], project_id="demo")
        assert holder is not None and holder.run_id == handle.run_id, "the precondition"

        seen: List[Dict[str, Any]] = []
        original_merge = finish_module.merge

        def observe_then_merge(*args: Any, **kwargs: Any) -> Any:
            home, task_id = world["home"], world["task_id"]
            before = read_task_lock_holder(home, task_id, project_id="demo")
            live = {run.run_id for run in live_runs(home)}
            # What `conclude_run` does to every lock once a run ends: the sweep that
            # deleted the borrowed lock in the incident.
            release_stale_locks(home)
            after = read_task_lock_holder(home, task_id, project_id="demo")
            status = read_finish_status(home, task_id, "demo")
            seen.append(
                {
                    "before": before,
                    "after": after,
                    "live": live,
                    "state": status.state if status else None,
                }
            )
            return original_merge(*args, **kwargs)

        monkeypatch.setattr(finish_module, "merge", observe_then_merge)

        result = finish(world)

        assert result.outcome == FINISHED, result.render()
        assert landed(world["root"], result)
        assert the_task(world).lifecycle is Lifecycle.CLOSED
        assert len(seen) == 1
        observed = seen[0]
        # a1: its own lock, attributed to no run.
        assert observed["before"] is not None
        assert observed["before"].kind == "finish"
        assert not observed["before"].run_id
        # a2: the session has concluded, the sweep ran, and the lock is still there.
        assert handle.run_id not in observed["live"]
        assert observed["after"] is not None and observed["after"].kind == "finish"
        assert observed["state"] == RUNNING
        # The session was stood down properly rather than run over.
        assert "Stood down" in (result_for(world, handle.run_id).body or "")
        # And the finish released what it took.
        assert read_task_lock_holder(world["home"], world["task_id"], project_id="demo") is None


class TestSpawnFinishGrantsRatherThanInherits:
    @pytest.fixture
    def captured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []

        def fake_popen(argv: List[str], **kwargs: Any) -> Any:
            calls.append({"argv": argv, "env": kwargs.get("env")})
            return SimpleNamespace(pid=1)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        leak_a_finished_runs_identity(tmp_path, monkeypatch)
        return calls

    def spawn(self, tmp_path: Path, **kwargs: Any) -> None:
        project = SimpleNamespace(id="demo", root=tmp_path)
        assert spawn_finish(
            project=project,  # type: ignore[arg-type]
            task_id="task-536",
            approver="Jeff Posey",
            home=tmp_path,
            **kwargs,
        )

    def test_an_approval_finish_inherits_no_run_identity(
        self, tmp_path: Path, captured: List[Dict[str, Any]]
    ) -> None:
        self.spawn(tmp_path)

        env = captured[0]["env"]
        assert env is not None, "None would hand the child the server's whole environment"
        assert not set(RUN_IDENTITY_VARS) & set(env)
        assert env.get("PATH") == os.environ.get("PATH")
        assert "--posture-release" not in captured[0]["argv"]

    def test_a_posture_finish_is_granted_only_its_own_run_id(
        self, tmp_path: Path, captured: List[Dict[str, Any]]
    ) -> None:
        self.spawn(tmp_path, posture_run_id="run_9bf70b6d")

        env = captured[0]["env"]
        assert env[RUN_ID_ENV] == "run_9bf70b6d"
        assert RUN_DIR_ENV not in env and CREDENTIAL_ENV not in env
        assert "--posture-release" in captured[0]["argv"]


class TestTheServerIsNeverARun:
    def test_serve_drops_an_inherited_identity_before_it_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from agentjobs.cli import app

        leak_a_finished_runs_identity(tmp_path, monkeypatch)
        seen: Dict[str, Any] = {}

        def fake_run(*args: Any, **kwargs: Any) -> None:
            seen.update({name: os.environ.get(name) for name in RUN_IDENTITY_VARS})

        monkeypatch.setattr("uvicorn.run", fake_run)

        outcome = CliRunner().invoke(app, ["serve", "--port", "18999"])

        assert outcome.exit_code == 0, outcome.output
        assert seen == {name: None for name in RUN_IDENTITY_VARS}
        assert "Dropped an inherited run identity" in outcome.output
        assert "not-a-real-token" not in outcome.output

    def test_the_finish_restarts_the_server_without_its_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.dispatch.finish import restart_server

        monkeypatch.setenv(RUN_ID_ENV, "run_9bf70b6d")
        monkeypatch.setenv(CREDENTIAL_ENV, "run_9bf70b6d.secret")
        seen: Dict[str, Any] = {}

        def fake_run_command(argv: Any, **kwargs: Any) -> Any:
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(finish_module, "run_command", fake_run_command)
        plan = SimpleNamespace(root=tmp_path)
        directory = SimpleNamespace(path=tmp_path)

        step = restart_server(
            plan,  # type: ignore[arg-type]
            ["src/agentjobs/cli.py"],
            finish_module.FinishSettings(enabled=True, restart=["launcher.cmd"]),
            directory,  # type: ignore[arg-type]
        )

        assert step.ok
        assert seen["env"] is not None
        assert not set(RUN_IDENTITY_VARS) & set(seen["env"])
