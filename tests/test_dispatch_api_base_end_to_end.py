"""What a dispatched agent is actually told, on every path that can start one.

test_dispatch_address.py pins the resolver. These pin the wiring, which is where
task-154 lived: the resolver could have been perfect and every dispatch would still have
named ``:8765``, because the endpoint passed nothing and a default was waiting at each
level to fill the gap.

So each test here dispatches for real and reads the address back out of the *recorded*
argv -- the verbatim command line in the task's ``dispatch`` log entry. That is the copy
the agent process received, which is the only copy that decides whether it can reach
AgentJobs at all.

Both halves are checked together on purpose. ``{api_base}`` is a supported argv
placeholder as well as a phrase inside ``{prompt}``, so a fix applied to one leaves a
second, wrong copy in every runner template that interpolates the other.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.cli import app as cli_app
from agentjobs.dispatch.address import API_BASE_ENV, DEFAULT_API_BASE
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

STUB = "import sys\nprint('started', flush=True)\n"
"""A runner that starts, prints, and exits. It never reads the address it is given.

Which is the point: this suite is about what was *handed over*, recorded before the
process was started. A stub that tried to call back would be testing httpx.
"""


# ----- fixtures ---------------------------------------------------------------


def build_project(root: Path) -> None:
    """A git-clean project AgentJobs can dispatch from."""
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


def write_dispatch_config(
    home: Path, tmp_path: Path, *, project_id: str, api_base: str | None = None
) -> None:
    """A runner that carries the address twice: inside the prompt, and on its own.

    ``{api_base}`` as a bare argv element is how an operator passes the address to a CLI
    that takes it as a flag, and it is substituted from the same value as the prompt. A
    template using it is the case that a prompt-only fix silently leaves broken.
    """
    stub = tmp_path / "stub_runner.py"
    stub.write_text(STUB, encoding="utf-8")
    config: Dict[str, object] = {
        "version": 1,
        "enabled": True,
        "runners": {
            "fake": {
                "argv": [sys.executable, str(stub), "--api", "{api_base}", "{prompt}"],
                "actor": "claude",
            }
        },
        "projects": {project_id: {"enabled": True, "runner": "fake"}},
    }
    if api_base is not None:
        config["api_base"] = api_base
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def seed_task(root: Path) -> str:
    """A ready task whose newest log entry is a human's, so dispatch is permitted."""
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.add_log_entry(task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go.")
    return task.id


def recorded_argv(root: Path, task_id: str) -> List[str]:
    """The argv AgentJobs recorded for the newest run of ``task_id``.

    Read from the task file rather than from the response, because this is the artefact
    a human debugging a silent run actually opens.
    """
    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.get_task(task_id)
    assert task is not None
    dispatches = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
    assert dispatches, f"{task_id} has no dispatch entry"
    argv = dispatches[-1].data.get("argv")
    assert isinstance(argv, list)
    return [str(element) for element in argv]


def addresses_in(argv: List[str]) -> Tuple[str, str]:
    """The ``{api_base}`` element and the address named inside the prompt element."""
    flagged = argv[argv.index("--api") + 1]
    prompt = next(element for element in argv if "AgentJobs is serving at" in element)
    # Split on the sentence that follows, not on the full stop: an address is mostly
    # full stops, and a helper that stops at the first one reports "http://127".
    named = prompt.split("AgentJobs is serving at ", 1)[1].split(". Read the task record", 1)[0]
    return flagged, named.strip()


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch) -> Iterator[Tuple[Path, Path]]:
    """A registered, git-clean project plus a throwaway AgentJobs home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.delenv(API_BASE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "sandbox"
    build_project(root)
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    yield root, home
    reset_dependency_cache()


# ----- the HTTP path ----------------------------------------------------------


class TestDispatchOverHttp:
    """sc-1: the address is the server's own, in the prompt and in the placeholder."""

    def test_a_server_on_a_non_default_port_names_that_port(self, sandbox, tmp_path: Path) -> None:
        root, home = sandbox
        write_dispatch_config(home, tmp_path, project_id="sandbox")
        task_id = seed_task(root)

        with TestClient(app, base_url="http://127.0.0.1:8901") as client:
            response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})
        assert response.status_code == 202, response.text

        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == "http://127.0.0.1:8901"
        assert named == "http://127.0.0.1:8901"

    def test_the_serving_address_beats_a_configured_one(self, sandbox, tmp_path: Path) -> None:
        """A declaration that disagrees with the socket answering the call is stale.

        The realistic case is a machine whose dispatch.yaml still names the port it used
        to serve on. The request cannot be wrong about which server received it.
        """
        root, home = sandbox
        write_dispatch_config(home, tmp_path, project_id="sandbox", api_base="http://host:9999")
        task_id = seed_task(root)

        with TestClient(app, base_url="http://127.0.0.1:8901") as client:
            response = client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})
        assert response.status_code == 202, response.text

        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == "http://127.0.0.1:8901"
        assert named == "http://127.0.0.1:8901"

    def test_the_default_port_no_longer_leaks_into_a_run_from_another_one(
        self, sandbox, tmp_path: Path
    ) -> None:
        """The regression itself, stated as the observation that opened task-154."""
        root, home = sandbox
        write_dispatch_config(home, tmp_path, project_id="sandbox")
        task_id = seed_task(root)

        with TestClient(app, base_url="http://127.0.0.1:8901") as client:
            client.post(f"/api/projects/sandbox/tasks/{task_id}/dispatch", json={})

        assert DEFAULT_API_BASE not in " ".join(recorded_argv(root, task_id))


class TestAutoDispatchAgrees:
    """sc-3: approving through the UI must not tell an agent something different."""

    def test_an_auto_dispatched_run_gets_the_same_address_as_a_manual_one(
        self, sandbox, tmp_path: Path
    ) -> None:
        root, home = sandbox
        write_dispatch_config(home, tmp_path, project_id="sandbox")
        raw = yaml.safe_load((home / "dispatch.yaml").read_text(encoding="utf-8"))
        raw["projects"]["sandbox"]["auto_dispatch"] = True
        (home / "dispatch.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        manager = TaskManager(TaskStorage(root / "tasks"))
        task_id = seed_task(root)
        manager.claim_task(task_id, agent="claude")

        with TestClient(app, base_url="http://127.0.0.1:8901") as client:
            response = client.post(
                f"/api/projects/sandbox/tasks/{task_id}/approve",
                json={"user": "Jeff Posey"},
            )
        assert response.status_code == 200, response.text

        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == "http://127.0.0.1:8901"
        assert named == "http://127.0.0.1:8901"


# ----- the CLI path -----------------------------------------------------------


class TestDispatchFromTheCli:
    """sc-2: the path with no request to derive anything from must still be right."""

    def test_it_uses_the_address_this_machine_declared(self, sandbox, tmp_path: Path) -> None:
        root, home = sandbox
        write_dispatch_config(
            home, tmp_path, project_id="sandbox", api_base="http://localhost:8876"
        )
        task_id = seed_task(root)

        result = CliRunner().invoke(cli_app, ["dispatch", "run", task_id, "--project", "sandbox"])

        assert result.exit_code == 0, result.output
        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == "http://localhost:8876"
        assert named == "http://localhost:8876"

    def test_it_prints_the_address_it_resolved(self, sandbox, tmp_path: Path) -> None:
        """Because a wrong one is otherwise silent until the run has already gone quiet."""
        root, home = sandbox
        write_dispatch_config(
            home, tmp_path, project_id="sandbox", api_base="http://localhost:8876"
        )
        task_id = seed_task(root)

        result = CliRunner().invoke(cli_app, ["dispatch", "run", task_id, "--project", "sandbox"])

        assert "http://localhost:8876" in result.output

    def test_the_environment_overrides_the_file(self, sandbox, tmp_path: Path, monkeypatch) -> None:
        root, home = sandbox
        write_dispatch_config(
            home, tmp_path, project_id="sandbox", api_base="http://localhost:8876"
        )
        monkeypatch.setenv(API_BASE_ENV, "http://127.0.0.1:9001")
        task_id = seed_task(root)

        result = CliRunner().invoke(cli_app, ["dispatch", "run", task_id, "--project", "sandbox"])

        assert result.exit_code == 0, result.output
        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == "http://127.0.0.1:9001"
        assert named == "http://127.0.0.1:9001"

    def test_a_machine_that_declared_nothing_still_gets_the_documented_default(
        self, sandbox, tmp_path: Path
    ) -> None:
        """Unchanged behaviour for the one case the old literal was right about."""
        root, home = sandbox
        write_dispatch_config(home, tmp_path, project_id="sandbox")
        task_id = seed_task(root)

        result = CliRunner().invoke(cli_app, ["dispatch", "run", task_id, "--project", "sandbox"])

        assert result.exit_code == 0, result.output
        flagged, _ = addresses_in(recorded_argv(root, task_id))
        assert flagged == DEFAULT_API_BASE


# ----- a real server on a real socket -----------------------------------------


class TestAgainstARealServer:
    """sc-4: a TestClient declares its address; uvicorn's socket reports one.

    Worth the ~5 seconds a subprocess costs. ``scope["server"]`` is filled in by the
    ASGI server from the listening socket, and TestClient synthesises it from the
    base_url it was handed -- so every test above would pass against an implementation
    that read the ``Host`` header instead, and that implementation is wrong behind the
    loopback proxy this dashboard is actually published through.
    """

    def test_the_prompt_names_the_port_uvicorn_bound(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "sandbox"
        build_project(root)
        ProjectRegistry(home=home).add(root, project_id="sandbox")
        write_dispatch_config(home, tmp_path, project_id="sandbox", api_base="http://host:9999")
        task_id = seed_task(root)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        assert port != 8765, "the ephemeral port must not be the one being defaulted to"

        env = dict(os.environ)
        env["AGENTJOBS_HOME"] = str(home)
        env.pop(TASKS_DIR_ENV, None)
        env.pop("AGENTJOBS_PROJECT_ROOT", None)
        env.pop(API_BASE_ENV, None)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agentjobs.api.main:app", "--port", str(port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:  # pragma: no cover - only on a very slow machine
                pytest.skip("AgentJobs service did not start in time")

            response = httpx.post(
                f"{url}/api/projects/sandbox/tasks/{task_id}/dispatch", json={}, timeout=30
            )
            assert response.status_code == 202, response.text
        finally:
            process.terminate()
            process.wait(timeout=30)

        flagged, named = addresses_in(recorded_argv(root, task_id))
        assert flagged == url
        assert named == url
