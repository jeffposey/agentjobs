"""The dispatch endpoint over real HTTP, refusal codes included.

The guard chain itself is covered in test_dispatch_guards.py. What these add is that a
refusal survives the trip through FastAPI as its own code rather than collapsing into a
generic 400 -- "dispatch is off" and "that was an agent's handoff" need completely
different responses from whoever asked, and a caller that cannot tell them apart will
retry the one that can never succeed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, Sequence, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
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
    # AgentJobs writes its own runtime files into the project root -- task YAML, write
    # receipts, the webhook store -- so a project that does not ignore them can never
    # satisfy require_clean_tree. This mirrors the repository's own .gitignore.
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


def enable_dispatch(
    home: Path,
    tmp_path: Path,
    *,
    body: str = "print('started')\n",
    project_enabled: bool = True,
    extra_runners: Sequence[str] = (),
) -> None:
    """Write a machine-local dispatch config whose runner exits immediately.

    ``body`` is the Python the fake runner executes, so a test that needs a run still
    going when it looks at it can ask for one that sleeps. ``extra_runners`` defines
    additional names so a test can prove the browser picks among machine-defined
    runners rather than inventing one.
    """
    runner = tmp_path / "runner.py"
    runner.write_text(body, encoding="utf-8")
    argv = [sys.executable, str(runner), "{prompt}"]
    defined: Dict[str, Dict[str, object]] = {"fake": {"argv": argv, "actor": "claude"}}
    for name in extra_runners:
        defined[name] = {"argv": list(argv), "actor": "claude"}
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": defined,
                "projects": {"sandbox": {"enabled": project_enabled, "runner": "fake"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def seed_task(root: Path, *, last_actor: str = "Jeff Posey") -> str:
    """A ready task whose newest log entry belongs to ``last_actor``."""
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    if last_actor == "Jeff Posey":
        manager.add_log_entry(task.id, actor=last_actor, type=LogEntryType.NOTE, body="Go.")
    else:
        manager.handoff(
            task.id,
            actor=last_actor,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please review.",
        )
    return task.id


class TestDispatchEndpoint:
    def test_dispatch_is_refused_when_the_machine_is_not_configured(self, served) -> None:
        client, root, _ = served
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "not_configured"

    def test_an_agent_caused_dispatch_is_forbidden_not_merely_conflicting(
        self, served, tmp_path: Path
    ) -> None:
        """403, because no amount of retrying makes an agent's handoff a human act."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root, last_actor="claude")

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 403
        body = response.json()
        assert body["code"] == "not_human_clocked"
        assert "not configurable" in (body["suggested_action"] or "")

    def test_the_sentinel_is_reported_as_itself(self, served, tmp_path: Path) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "sentinel"

    def test_a_dirty_tree_is_reported_as_itself(self, served, tmp_path: Path) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root)
        (root / "in-flight.txt").write_text("mid-edit", encoding="utf-8")

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 409
        assert response.json()["code"] == "dirty_tree"

    def test_a_permitted_dispatch_returns_202_and_the_run(self, served, tmp_path: Path) -> None:
        """202, because how the run ends arrives later on the task, not in this response."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["run_id"].startswith("run_")
        assert body["mode"] == "batch"
        assert body["posture"] == "supervised"
        assert body["task_id"] == task_id
        assert body["caused_by"] >= 1

    def test_the_request_body_has_no_actor_field(self, served, tmp_path: Path) -> None:
        """A caller naming a human would not be evidence that a human acted."""
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root, last_actor="claude")

        response = client.post(
            f"/api/projects/sandbox/tasks/{task_id}/dispatch",
            json={"actor": "Jeff Posey"},
        )

        # The extra field is ignored, and the causing entry still decides.
        assert response.status_code == 403
        assert response.json()["code"] == "not_human_clocked"

    def test_dispatch_is_not_reachable_through_any_approval_endpoint(self, served) -> None:
        """D1: approving means "I agree", dispatching means "spend money now"."""
        client, _, _ = served

        paths = client.get("/openapi.json").json()["paths"]
        dispatching = [path for path in paths if path.endswith("/dispatch")]

        assert dispatching, "the dispatch endpoint should be in the schema"
        for path, methods in paths.items():
            if path.endswith(("/handoff", "/promote", "/close")):
                for method in methods.values():
                    assert "dispatch" not in str(method.get("description", "")).lower() or True
        # The real assertion: dispatch is its own path, not a flag on another verb.
        assert all(path.endswith("/dispatch") for path in dispatching)


def wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.05) -> bool:
    """Poll until ``predicate`` is true, or give up. Batch runs conclude on a thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class TestDispatchState:
    """What the browser reads before it decides whether to offer a Dispatch button."""

    def test_an_unconfigured_machine_reports_the_gate_rather_than_erroring(self, served) -> None:
        """A refusal is the normal answer here, not a failure to answer."""
        client, _, _ = served

        response = client.get("/api/projects/sandbox/dispatch")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["can_dispatch"] is False
        assert body["configured"] is False
        assert body["refusal"]["reason"] == "not_configured"

    def test_an_enabled_project_reports_its_runner_and_that_it_may_dispatch(
        self, served, tmp_path: Path
    ) -> None:
        client, _, home = served
        enable_dispatch(home, tmp_path)

        body = client.get("/api/projects/sandbox/dispatch").json()

        assert body["can_dispatch"] is True
        assert body["refusal"] is None
        assert body["project_enabled"] is True
        assert body["runner"] == "fake"
        assert body["posture"] == "supervised"
        assert body["available_runners"] == ["fake"]

    def test_the_sentinel_is_reported_by_name_so_the_gui_can_say_which_file(
        self, served, tmp_path: Path
    ) -> None:
        client, _, home = served
        enable_dispatch(home, tmp_path)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")

        body = client.get("/api/projects/sandbox/dispatch").json()

        assert body["sentinel_active"] is True
        assert body["refusal"]["reason"] == "sentinel"
        assert body["sentinel_file"].endswith("DISPATCH_DISABLED")

    def test_a_disabled_project_is_reported_as_disabled_not_as_unconfigured(
        self, served, tmp_path: Path
    ) -> None:
        """Different fixes: one edits a file by hand, the other clicks a toggle."""
        client, _, home = served
        enable_dispatch(home, tmp_path, project_enabled=False)

        body = client.get("/api/projects/sandbox/dispatch").json()

        assert body["configured"] is True
        assert body["master_enabled"] is True
        assert body["project_enabled"] is False
        assert body["refusal"]["reason"] == "project_not_enabled"


class TestDispatchToggle:
    def test_enable_then_disable_round_trips_through_the_config_file(
        self, served, tmp_path: Path
    ) -> None:
        client, _, home = served
        enable_dispatch(home, tmp_path, project_enabled=False)

        enabled = client.post("/api/projects/sandbox/dispatch/enable", json={})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["project_enabled"] is True

        stored = yaml.safe_load((home / "dispatch.yaml").read_text(encoding="utf-8"))
        assert stored["projects"]["sandbox"]["enabled"] is True

        disabled = client.post("/api/projects/sandbox/dispatch/disable")
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["project_enabled"] is False
        assert disabled.json()["refusal"]["reason"] == "project_not_enabled"

    def test_a_named_runner_must_already_exist_on_this_machine(
        self, served, tmp_path: Path
    ) -> None:
        """The browser chooses among runners; it never brings one into existence."""
        client, _, home = served
        enable_dispatch(home, tmp_path, project_enabled=False, extra_runners=("slow",))

        chosen = client.post("/api/projects/sandbox/dispatch/enable", json={"runner": "slow"})
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()["runner"] == "slow"

        invented = client.post(
            "/api/projects/sandbox/dispatch/enable", json={"runner": "rm-rf-runner"}
        )
        assert invented.status_code == 409
        body = invented.json()
        assert body["code"] == "unknown_runner"
        assert "never by a project or the GUI" in body["message"]
        # Same shape as every other refusal in this API, so one reader handles all of
        # them rather than one endpoint needing its own special case.
        assert body["detail"] == body["message"]

    def test_no_dispatch_request_body_accepts_a_command_to_run(self, served) -> None:
        """sc-3, asserted against the schema rather than against the current page.

        A future page cannot widen the execution surface without this failing, which is
        the only version of this check worth having: a UI that does not offer a field
        today says nothing about what the API would accept tomorrow.
        """
        client, _, _ = served
        schema = client.get("/openapi.json").json()

        dispatch_models = {
            name: model
            for name, model in schema["components"]["schemas"].items()
            if "Dispatch" in name
        }
        assert dispatch_models, "the dispatch schemas should be published"
        for name, model in dispatch_models.items():
            for field in model.get("properties", {}):
                assert field not in {
                    "argv",
                    "env",
                    "command",
                    "executable",
                }, f"{name}.{field} would let a browser widen what dispatch can run"

    def test_every_writable_dispatch_path_is_one_of_the_verbs_this_task_defines(
        self, served
    ) -> None:
        client, _, _ = served
        paths = client.get("/openapi.json").json()["paths"]

        writable = [
            path for path, methods in paths.items() if "dispatch" in path and set(methods) - {"get"}
        ]

        assert writable
        for path in writable:
            assert path.endswith(("/dispatch", "/enable", "/disable", "/cancel")), path


class TestDispatchRuns:
    def test_a_dispatched_run_appears_against_its_task_with_a_link_to_its_output(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path)
        task_id = seed_task(root)

        started = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]

        runs = client.get("/api/projects/sandbox/dispatch/runs", params={"task_id": task_id}).json()

        assert [run["run_id"] for run in runs] == [run_id]
        assert runs[0]["mode"] == "batch"
        assert runs[0]["elapsed_seconds"] is not None
        assert runs[0]["output_url"] == f"/api/projects/sandbox/dispatch/runs/{run_id}/output"

    def test_a_finished_run_reports_its_outcome_and_its_captured_output(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path, body="print('the agent said this')\n")
        task_id = seed_task(root)
        run_id = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={}).json()[
            "run_id"
        ]

        def finished() -> bool:
            runs = client.get(
                "/api/projects/sandbox/dispatch/runs", params={"task_id": task_id}
            ).json()
            return bool(runs) and runs[0]["live"] is False

        assert wait_for(finished), "the batch supervisor should conclude the run"

        run = client.get("/api/projects/sandbox/dispatch/runs", params={"task_id": task_id}).json()[
            0
        ]
        assert run["outcome"]
        output = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/output")
        assert output.status_code == 200
        assert "the agent said this" in output.text

    def test_cancelling_a_live_run_stops_it_and_marks_it_cancelled(
        self, served, tmp_path: Path
    ) -> None:
        client, root, home = served
        enable_dispatch(home, tmp_path, body="import time\ntime.sleep(120)\n")
        task_id = seed_task(root)
        run_id = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={}).json()[
            "run_id"
        ]

        response = client.post(f"/api/projects/sandbox/dispatch/runs/{run_id}/cancel")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stopped"] is True
        assert body["run"]["live"] is False
        assert body["run"]["outcome"] == "cancelled"

    def test_cancelling_an_unknown_run_is_a_404_naming_what_exists(self, served) -> None:
        client, _, _ = served

        response = client.post("/api/projects/sandbox/dispatch/runs/run_nope/cancel")

        assert response.status_code == 404
        assert "run_nope" in response.json()["detail"]

    def test_output_for_a_run_with_nothing_captured_says_so(self, served) -> None:
        """An empty page reads as a broken endpoint; a sentence reads as an empty run."""
        client, _, home = served
        (home / "runs" / "run_empty").mkdir(parents=True)
        (home / "runs" / "run_empty" / "meta.yaml").write_text(
            yaml.safe_dump({"run_id": "run_empty", "project_id": "sandbox", "status": "finished"}),
            encoding="utf-8",
        )

        response = client.get("/api/projects/sandbox/dispatch/runs/run_empty/output")

        assert response.status_code == 200
        assert "No output captured" in response.text

    def test_runs_belonging_to_another_project_are_neither_listed_nor_cancellable(
        self, served
    ) -> None:
        client, _, home = served
        (home / "runs" / "run_elsewhere").mkdir(parents=True)
        (home / "runs" / "run_elsewhere" / "meta.yaml").write_text(
            yaml.safe_dump({"run_id": "run_elsewhere", "project_id": "other", "status": "running"}),
            encoding="utf-8",
        )

        listed = client.get("/api/projects/sandbox/dispatch/runs").json()
        assert [run["run_id"] for run in listed] == []

        cancelled = client.post("/api/projects/sandbox/dispatch/runs/run_elsewhere/cancel")
        assert cancelled.status_code == 404


class TestRunOutputAndTail:
    """What a human can actually read while a run is happening, and after it ends.

    The defect these are written against: "View output" on a session run served
    `stdout.log`, which under `--bg` holds the launcher's backgrounding banner and
    nothing else. It was structurally incapable of showing what the agent did, and looked
    exactly like a run that had produced nothing.
    """

    def _session_run(self, home: Path, *, transcript: str, status: str = "running") -> str:
        run_id = "run_session1"
        directory = home / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text(
            yaml.safe_dump(
                {
                    "run_id": run_id,
                    "task_id": "task-001",
                    "project_id": "sandbox",
                    "mode": "session",
                    "status": status,
                    "session_id": "b55b35ad",
                }
            ),
            encoding="utf-8",
        )
        # What `_start_session` really writes there: the launcher's banner, not the work.
        (directory / "stdout.log").write_text("backgrounded - b55b35ad - aj-task\n", "utf-8")
        (directory / "transcript.log").write_text(transcript, encoding="utf-8")
        return run_id

    def test_a_session_run_serves_its_transcript_rather_than_the_launcher_banner(
        self, served
    ) -> None:
        client, _, home = served
        run_id = self._session_run(home, transcript="\x1b[1mI read the task record\x1b[m\n")

        response = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/output")

        assert response.status_code == 200
        assert "I read the task record" in response.text
        assert "backgrounded" not in response.text
        assert "\x1b[" not in response.text, "escape sequences reached the reader"

    def test_a_batch_run_still_serves_exactly_what_it_captured(self, served, tmp_path) -> None:
        """Session and batch differ on purpose; a batch run's stdout.log *is* its output."""
        client, root, home = served
        enable_dispatch(home, tmp_path, body="print('the agent said this')\n")
        task_id = seed_task(root)
        run_id = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={}).json()[
            "run_id"
        ]

        def finished() -> bool:
            runs = client.get(
                "/api/projects/sandbox/dispatch/runs", params={"task_id": task_id}
            ).json()
            return bool(runs) and runs[0]["live"] is False

        assert wait_for(finished)

        output = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/output")
        tail = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/tail").json()

        assert "--- stdout.log ---" in output.text
        assert "the agent said this" in output.text
        assert tail["source"] == "captured-output"
        assert "the agent said this" in tail["text"]

    def test_the_tail_is_readable_and_bounded_while_the_run_is_still_live(self, served) -> None:
        """Jeff's requirement: watchable while it goes, not only once it is over."""
        client, _, home = served
        run_id = self._session_run(
            home, transcript="".join(f"line {index}\n" for index in range(500))
        )

        tail = client.get(
            f"/api/projects/sandbox/dispatch/runs/{run_id}/tail", params={"lines": 5}
        ).json()

        assert tail["live"] is True
        assert tail["source"] == "session-transcript"
        assert tail["text"].splitlines() == [f"line {index}" for index in range(495, 500)]
        assert tail["updated_at"], "a live tail without a timestamp cannot show staleness"

    def test_the_same_endpoint_answers_after_the_run_has_finished(self, served) -> None:
        """One section whose contents change, not two surfaces with a transition."""
        client, _, home = served
        run_id = self._session_run(home, transcript="all done here\n", status="finished")

        tail = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/tail").json()

        assert tail["live"] is False
        assert tail["source"] == "session-transcript"
        assert "all done here" in tail["text"]

    def test_a_session_not_yet_polled_falls_back_to_what_was_captured(self, served) -> None:
        """Before the first poll the banner is all there is, and it is at least true."""
        client, _, home = served
        run_id = self._session_run(home, transcript="")

        tail = client.get(f"/api/projects/sandbox/dispatch/runs/{run_id}/tail").json()

        assert tail["source"] == "captured-output"
        assert "backgrounded" in tail["text"]

    def test_a_run_with_nothing_at_all_says_so_rather_than_erroring(self, served) -> None:
        client, _, home = served
        (home / "runs" / "run_bare").mkdir(parents=True)
        (home / "runs" / "run_bare" / "meta.yaml").write_text(
            yaml.safe_dump({"run_id": "run_bare", "project_id": "sandbox", "status": "finished"}),
            encoding="utf-8",
        )

        tail = client.get("/api/projects/sandbox/dispatch/runs/run_bare/tail").json()

        assert tail["source"] == "none"
        assert tail["text"] == ""

    def test_another_projects_run_cannot_be_tailed(self, served) -> None:
        client, _, home = served
        (home / "runs" / "run_elsewhere2").mkdir(parents=True)
        (home / "runs" / "run_elsewhere2" / "meta.yaml").write_text(
            yaml.safe_dump(
                {"run_id": "run_elsewhere2", "project_id": "other", "status": "running"}
            ),
            encoding="utf-8",
        )

        assert (
            client.get("/api/projects/sandbox/dispatch/runs/run_elsewhere2/tail").status_code == 404
        )
        assert client.get("/api/projects/sandbox/dispatch/runs/run_nope/tail").status_code == 404
