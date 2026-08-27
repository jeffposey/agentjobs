"""Reading a scripted finish while it is still happening (task-321).

The finish itself is covered in test_dispatch_finish.py. What these add is the surface
that was missing: whether a page asking "what is happening to this task" gets an answer
that is true at each of the states a finish passes through -- including the two that
have no outcome written anywhere, the second before the process has created anything and
the case where it died without writing an ending.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch import finish as finish_module
from agentjobs.dispatch.finish_status import (
    INTERRUPTED,
    STARTING,
    STEP_ORDER,
    FinishStatus,
    finish_output,
    read_finish_status,
)
from agentjobs.dispatch.ledger import KIND_FINISH, locks_root
from agentjobs.projects import ProjectRegistry

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [{"name": "Jeff Posey", "kind": "human"}, {"name": "claude", "kind": "agent"}],
    "default_user": "Jeff Posey",
}


def write_finish(
    home: Path,
    finish_id: str,
    *,
    task_id: str = "task-001",
    project_id: str = "sandbox",
    outcome: str = "running",
    started_at: Optional[str] = None,
    **meta: Any,
) -> Path:
    """A finish directory as ``FinishDirectory`` would have left it."""
    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "finish_id": finish_id,
        "task_id": task_id,
        "project_id": project_id,
        "outcome": outcome,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    }
    payload.update(meta)
    (directory / "meta.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return directory


def record(directory: Path, kind: str, **fields: Any) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})
    with (directory / "phases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def hold_lock(home: Path, task_id: str, *, pid: int, kind: str = KIND_FINISH) -> None:
    """Write the lock file a finish holds for the whole of its attempt."""
    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{task_id}.lock").write_text(
        f"pid={pid} run= kind={kind} finish=fin_x started=2026-01-01T00:00:00+00:00",
        encoding="utf-8",
    )


DEAD_PID = 999_999
"""A pid nothing can be running under. Windows and POSIX both report it absent."""


class TestReadingOneFinish:
    def test_a_task_with_no_finish_reads_as_nothing_at_all(self, tmp_path: Path) -> None:
        assert read_finish_status(tmp_path, "task-001", "sandbox") is None

    def test_the_marker_alone_reports_a_finish_starting(self, tmp_path: Path) -> None:
        """The one-to-two-second window between the click and the first write.

        This is the case the whole marker exists for: a page that polled here and was
        told "nothing" would stop polling and show nothing for the following three
        minutes of a finish it had itself started.
        """
        finish_module.write_spawn_marker(
            tmp_path, "task-001", project_id="sandbox", approver="Jeff Posey"
        )

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == STARTING
        assert status.live is True
        assert status.current_step == STEP_ORDER[0]

    def test_a_marker_nothing_ever_picked_up_stops_claiming_to_be_live(
        self, tmp_path: Path
    ) -> None:
        spawn = tmp_path / "finishes" / "spawn"
        spawn.mkdir(parents=True)
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        (spawn / "task-001.json").write_text(
            json.dumps({"task_id": "task-001", "started_at": stale.isoformat()}),
            encoding="utf-8",
        )

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == INTERRUPTED
        assert status.live is False

    def test_a_running_finish_reports_its_steps_and_the_one_in_flight(self, tmp_path: Path) -> None:
        directory = write_finish(tmp_path, "fin_a")
        record(directory, "finish_preflight", branch="feat/x", worktree=str(tmp_path / "w"))
        record(directory, "finish_step", step="preflight", ok=True, detail="feat/x", seconds=1.4)
        record(directory, "finish_step", step="runway", ok=True, detail="taken", seconds=0.0)
        hold_lock(tmp_path, "task-001", pid=os.getpid())

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == "running"
        assert status.live is True
        assert status.branch == "feat/x"
        assert [(step.name, step.state) for step in status.steps] == [
            ("preflight", "done"),
            ("runway", "done"),
            # Inferred from the fixed order: the step after the last one that landed.
            ("rebase", "running"),
        ]

    def test_gate_progress_comes_from_the_stages_the_gate_records(self, tmp_path: Path) -> None:
        directory = write_finish(tmp_path, "fin_b")
        record(directory, "finish_step", step="rebase", ok=True, detail="onto main", seconds=0.2)
        record(directory, "gate_started", stages=["black", "ruff", "pytest"], stages_total=3)
        record(directory, "gate_stage_finished", stage="black", index=1, total=3, seconds=0.6)
        record(directory, "gate_stage_started", stage="ruff", index=2, total=3)
        hold_lock(tmp_path, "task-001", pid=os.getpid())

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.gate is not None
        assert status.gate.running is True
        assert status.gate.stage == "ruff"
        assert (status.gate.stages_run, status.gate.stages_total) == (1, 3)

    def test_a_finish_that_died_mid_gate_is_interrupted_rather_than_forever_running(
        self, tmp_path: Path
    ) -> None:
        """The state that would otherwise render as a spinner nothing ever stops.

        Nothing writes it: by definition the process that would have was not there to.
        It is concluded from the lock, whose holder can be shown to be gone.
        """
        directory = write_finish(tmp_path, "fin_c")
        record(directory, "finish_step", step="preflight", ok=True, detail="feat/x", seconds=1.0)
        hold_lock(tmp_path, "task-001", pid=DEAD_PID)

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == INTERRUPTED
        assert status.live is False
        assert status.current_step == ""

    def test_a_finished_finish_reports_the_merge_and_stops_being_live(self, tmp_path: Path) -> None:
        started = datetime.now(timezone.utc) - timedelta(seconds=200)
        write_finish(
            tmp_path,
            "fin_d",
            outcome="finished",
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            merge_commit="7733b2b08c8138bba3171047fa25c847cf370ad9",
        )

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == "finished"
        assert status.live is False
        assert status.merged is True
        assert status.elapsed_seconds is not None and status.elapsed_seconds >= 199

    def test_an_escalation_says_which_step_stopped_it(self, tmp_path: Path) -> None:
        directory = write_finish(
            tmp_path,
            "fin_e",
            outcome="escalated",
            reason="gate_failed",
            stopped_at="gate",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        record(directory, "finish_step", step="rebase", ok=True, detail="onto main", seconds=0.2)
        record(directory, "finish_step", step="gate", ok=False, detail="exit 1", seconds=61.0)

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == "escalated"
        assert status.stopped_at == "gate"
        assert status.steps[-1].state == "stopped"

    def test_the_newest_finish_wins_over_an_earlier_attempt(self, tmp_path: Path) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        write_finish(
            tmp_path,
            "fin_old",
            outcome="escalated",
            started_at=old.isoformat(),
            finished_at=old.isoformat(),
            reason="gate_failed",
        )
        write_finish(
            tmp_path,
            "fin_new",
            outcome="finished",
            finished_at=datetime.now(timezone.utc).isoformat(),
            merge_commit="abc1234",
        )

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.finish_id == "fin_new"

    def test_a_marker_written_after_the_last_finish_means_the_next_one_is_starting(
        self, tmp_path: Path
    ) -> None:
        """A retry must not be reported using the previous attempt's outcome.

        Without this the reader who pressed Approve a second time would be shown the
        first attempt's escalation as though it were the answer about this one.
        """
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        write_finish(
            tmp_path,
            "fin_old",
            outcome="escalated",
            started_at=old.isoformat(),
            finished_at=old.isoformat(),
        )
        finish_module.write_spawn_marker(
            tmp_path, "task-001", project_id="sandbox", approver="Jeff Posey"
        )

        status = read_finish_status(tmp_path, "task-001", "sandbox")

        assert status is not None
        assert status.state == STARTING

    def test_another_task_s_finish_is_not_this_task_s(self, tmp_path: Path) -> None:
        write_finish(tmp_path, "fin_f", task_id="task-999")

        assert read_finish_status(tmp_path, "task-001", "sandbox") is None


class TestTheStepOrderMatchesTheSequence:
    def test_every_step_the_finish_records_is_one_this_module_knows(self) -> None:
        """The duplicated order is checked rather than trusted.

        ``STEP_ORDER`` is a copy of what ``_sequence`` runs, and a step added there
        without being added here would be rendered as nothing at all -- the finish would
        appear to skip it. The finish's own step names are string literals inside that
        function, so this reads them out of the source rather than importing a list that
        does not exist.
        """
        import inspect
        import re

        source = inspect.getsource(finish_module)
        recorded = set(re.findall(r'StepResult\(\s*"([a-z_]+)"', source))
        recorded |= set(re.findall(r'StepResult\(\s*\n\s*"([a-z_]+)"', source))
        # `[a-z_]+` rather than `[a-z]+`: a step name carrying an underscore --
        # `catch_up` is the first -- was invisible to this check, which would have let
        # through exactly the omission it exists to catch (task-297).
        # Equality rather than a subset, and it is the stronger half that matters: a
        # match that found nothing would pass a subset check while proving nothing.
        assert recorded == set(STEP_ORDER)


class TestTheMarkerIsWrittenBeforeAnythingIsSpawned:
    def test_the_marker_exists_by_the_time_the_process_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering is the feature, so it is the ordering that is asserted.

        A marker written *after* ``Popen`` would still exist a millisecond later and
        every state test above would pass -- and the approve request could still answer
        before it, which is the one moment it has to be there for.
        """
        seen: Dict[str, bool] = {}

        class FakePopen:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                seen["marker_existed"] = (
                    tmp_path / "finishes" / "spawn" / "task-001.json"
                ).is_file()

        monkeypatch.setattr(finish_module.subprocess, "Popen", FakePopen)

        class FakeProject:
            id = "sandbox"
            root = tmp_path

        finish_module.spawn_finish(
            project=FakeProject(),  # type: ignore[arg-type]
            task_id="task-001",
            approver="Jeff Posey",
            home=tmp_path,
        )

        assert seen["marker_existed"] is True


class TestStepsAreRecordedAsTheyLand:
    def test_appending_a_step_writes_it_to_the_finish_record(self, tmp_path: Path) -> None:
        """Not only at the end, which is what the table on the task always was.

        The recording rides on ``append`` rather than on a second call beside each one,
        precisely so a step cannot land in the table and not in the live view.
        """
        directory = finish_module.FinishDirectory.create(tmp_path, "task-001", "sandbox")
        steps = finish_module.StepLog(directory)

        steps.append(finish_module.StepResult("preflight", True, "feat/x", 1.4))
        steps.append(finish_module.StepResult("rebuild", True, "nothing to do", 0.0, skipped=True))

        assert [step.step for step in steps] == ["preflight", "rebuild"]
        status = read_finish_status(tmp_path, "task-001", "sandbox")
        assert status is not None
        assert [(step.name, step.state) for step in status.steps][:2] == [
            ("preflight", "done"),
            ("rebuild", "skipped"),
        ]

    def test_a_step_log_with_no_directory_is_still_a_list(self, tmp_path: Path) -> None:
        steps = finish_module.StepLog()
        steps.append(finish_module.StepResult("preflight", True, "feat/x", 1.0))
        assert len(steps) == 1


class TestTheOutputAPersonReads:
    def test_the_spawn_log_is_the_finish_s_output(self, tmp_path: Path) -> None:
        write_finish(tmp_path, "fin_g", outcome="finished")
        spawn = tmp_path / "finishes" / "spawn"
        spawn.mkdir(parents=True, exist_ok=True)
        (spawn / "task-001.log").write_text("task-001: finished\n  gate ok\n", encoding="utf-8")

        status = read_finish_status(tmp_path, "task-001", "sandbox")
        assert status is not None
        source, text, _ = finish_output(tmp_path, status)

        assert source == "finish-log"
        assert "gate ok" in text

    def test_a_finish_inside_a_session_falls_back_to_the_gate_s_own_log(
        self, tmp_path: Path
    ) -> None:
        """``--posture-release`` writes no spawn log; there is no session transcript."""
        directory = write_finish(tmp_path, "fin_h", outcome="finished")
        (directory / "gate.log").write_text("pytest ... 300 passed\n", encoding="utf-8")

        status = read_finish_status(tmp_path, "task-001", "sandbox")
        assert status is not None
        source, text, _ = finish_output(tmp_path, status)

        assert source == "gate-log"
        assert "300 passed" in text

    def test_a_running_finish_has_no_text_yet_and_says_so_by_being_empty(
        self, tmp_path: Path
    ) -> None:
        write_finish(tmp_path, "fin_i")
        hold_lock(tmp_path, "task-001", pid=os.getpid())

        status = read_finish_status(tmp_path, "task-001", "sandbox")
        assert status is not None
        source, text, _ = finish_output(tmp_path, status)

        assert (source, text) == ("none", "")


@pytest.fixture()
def served(tmp_path: Path, monkeypatch):
    """A served project over real HTTP, with a throwaway AgentJobs home."""
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
    ProjectRegistry(home=home).add(root, project_id="sandbox")

    with TestClient(app) as client:
        yield client, root, home

    reset_dependency_cache()


class TestOverHttp:
    def test_a_task_with_no_finish_answers_null_rather_than_404(self, served) -> None:
        """An absent finish is not a missing resource.

        A page polling every two seconds for the ordinary case must not be generating a
        404 each time; a reader who learns to ignore those will ignore a real one.
        """
        client, _, _ = served

        response = client.get("/api/projects/sandbox/dispatch/finishes/task-001")

        assert response.status_code == 200
        assert response.json() is None

    def test_a_running_finish_is_rendered_with_its_steps(self, served) -> None:
        client, _, home = served
        directory = write_finish(home, "fin_j")
        record(directory, "finish_preflight", branch="feat/x", worktree=str(home / "w"))
        record(directory, "finish_step", step="preflight", ok=True, detail="feat/x", seconds=1.4)
        hold_lock(home, "task-001", pid=os.getpid())

        body = client.get("/api/projects/sandbox/dispatch/finishes/task-001").json()

        assert body["state"] == "running"
        assert body["live"] is True
        assert body["branch"] == "feat/x"
        assert body["steps"][0]["name"] == "preflight"
        assert body["steps"][0]["state"] == "done"
        # The step meaning is what a reader who has never opened ENGINEERING.md gets.
        assert body["steps"][0]["meaning"]
        assert body["output_url"].endswith("/dispatch/finishes/task-001/output")

    def test_the_output_endpoint_answers_in_text(self, served) -> None:
        client, _, home = served
        write_finish(home, "fin_k", outcome="finished")
        spawn = home / "finishes" / "spawn"
        spawn.mkdir(parents=True, exist_ok=True)
        (spawn / "task-001.log").write_text("task-001: finished\n", encoding="utf-8")

        response = client.get("/api/projects/sandbox/dispatch/finishes/task-001/output")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "task-001: finished" in response.text

    def test_the_output_endpoint_explains_an_empty_one_rather_than_answering_blank(
        self, served
    ) -> None:
        client, _, home = served
        write_finish(home, "fin_l")
        hold_lock(home, "task-001", pid=os.getpid())

        response = client.get("/api/projects/sandbox/dispatch/finishes/task-001/output")

        assert response.status_code == 200
        assert "written no output yet" in response.text

    def test_the_elapsed_time_is_computed_by_the_server(self, served) -> None:
        """A phone reading this page is not on this machine's clock."""
        client, _, home = served
        started = datetime.now(timezone.utc) - timedelta(seconds=90)
        write_finish(home, "fin_m", started_at=started.isoformat())
        hold_lock(home, "task-001", pid=os.getpid())

        body = client.get("/api/projects/sandbox/dispatch/finishes/task-001").json()

        assert body["elapsed_seconds"] >= 89


def test_the_status_dataclass_is_read_only() -> None:
    """It describes what is on disk; nothing that reads it may change what it says."""
    status = FinishStatus(task_id="task-001", project_id="sandbox", state="running", live=True)
    with pytest.raises(Exception):
        status.state = "finished"  # type: ignore[misc]
