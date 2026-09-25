"""A task says whether it is a design pass or an implementation task (task-592).

`kind` is an optional top-level field: absent means `implementation`, so every record
written before it existed keeps its meaning. These cases walk it through every surface
the spec names -- model, store, REST create/patch/read/list, MCP create/update/read --
and hold the two properties the design depends on: absent reads as implementation, and a
reader older than the field is not broken by a value it does not know.

The CLI half lives in ``test_cli.py`` beside the other CLI cases.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Tuple

import anyio
import httpx
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import types
from pydantic import ValidationError

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.manager import TaskManager
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import Dependency, DependencyType, Task, TaskKind, kind_of, summary_of
from agentjobs.projects import ProjectRegistry
from support import task_store

ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
]


def op() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> Iterator[Tuple[ToolRegistry, TaskManager, TestClient]]:
    """The real app over one registered project, with MCP tools built on its client."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "solo"
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Solo",
                "tasks_directory": "tasks",
                "actors": ACTORS,
                "default_user": "Ada",
            }
        ),
        encoding="utf-8",
    )
    ProjectRegistry(home=tmp_path / "home").add(root, project_id="solo")
    manager = TaskManager(task_store(root / "tasks"))

    with TestClient(app) as http:
        client = TaskClient("http://testserver", client=http)
        yield build_registry(client), manager, http

    reset_dependency_cache()


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke a tool, validating its arguments and result against its own schemas."""

    async def run() -> Dict[str, Any]:
        definition = registry.get(name)
        jsonschema.validate(instance=dict(arguments), schema=definition.input_schema)
        result = await definition.handler(arguments)
        assert not isinstance(result, types.CallToolResult), "tool reported an error"
        _, structured = result
        return structured

    return anyio.run(run)


def _base(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": "task-001",
        "title": "Work",
        "created": "2026-09-25T00:00:00+00:00",
        "updated": "2026-09-25T00:00:00+00:00",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": "medium",
        "category": "general",
        "spec": {"summary": "Work.", "description": "Do the thing."},
        "queue_position": 100,
    }
    payload.update(overrides)
    return payload


class TestTheModel:
    def test_absent_is_none_and_reads_as_implementation(self) -> None:
        task = Task.model_validate(_base())
        assert task.kind is None
        assert kind_of(task) is TaskKind.IMPLEMENTATION

    def test_design_is_carried_and_summarised(self) -> None:
        task = Task.model_validate(_base(kind="design"))
        assert task.kind is TaskKind.DESIGN
        assert kind_of(task) is TaskKind.DESIGN
        assert summary_of(task).kind is TaskKind.DESIGN

    def test_an_unset_kind_does_not_appear_in_a_sparse_dump(self) -> None:
        """A record that never had a kind must serialise as it did before the field."""
        dumped = Task.model_validate(_base()).model_dump(mode="json", exclude_none=True)
        assert "kind" not in dumped

    def test_an_unknown_kind_is_refused_on_write(self) -> None:
        with pytest.raises(ValidationError):
            Task.model_validate(_base(kind="research"))


class TestAnOlderClient:
    def test_a_kind_the_client_does_not_declare_still_reads(self) -> None:
        """The widening this enum is designed for: a newer service serves `research`
        and a reader that only knows two kinds must keep the task, not fail on it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_base(kind="research"))

        with TaskClient(
            base_url="http://testserver", transport=httpx.MockTransport(handler)
        ) as client:
            task = client.get_task("task-001")

        assert task.kind == "research"
        assert task.title == "Work"


class TestTheStore:
    def test_kind_round_trips_through_the_database(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        manager.create_task(title="A design pass", description="Decide.", kind=TaskKind.DESIGN)
        manager.create_task(title="Build it", description="Build.")

        design, plain = sorted(manager.list_tasks(), key=lambda task: task.title)
        assert design.kind is TaskKind.DESIGN
        assert plain.kind is None
        summaries = {row.title: row.kind for row in manager.list_task_summaries()}
        assert summaries == {"A design pass": TaskKind.DESIGN, "Build it": None}

    def test_a_patch_sets_and_clears_it(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task = manager.create_task(title="Work", description="Do.")
        assert manager.update_task(task.id, kind=TaskKind.DESIGN).kind is TaskKind.DESIGN
        reread = manager.get_task(task.id)
        assert reread is not None and reread.kind is TaskKind.DESIGN
        assert manager.update_task(task.id, kind=None).kind is None

    def test_the_filter_treats_absent_as_implementation(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        manager.create_task(title="Design", description="d", kind=TaskKind.DESIGN)
        manager.create_task(title="Explicit", description="d", kind=TaskKind.IMPLEMENTATION)
        manager.create_task(title="Absent", description="d")

        def titles(kind: TaskKind) -> set[str]:
            return {row.title for row in manager.list_task_summaries(kind=kind)}

        assert titles(TaskKind.DESIGN) == {"Design"}
        assert titles(TaskKind.IMPLEMENTATION) == {"Explicit", "Absent"}
        assert {task.title for task in manager.list_tasks(kind=TaskKind.DESIGN)} == {"Design"}


class TestRest:
    def test_create_patch_read_and_list(self, service) -> None:
        _, _, http = service
        created = http.post(
            "/api/projects/solo/tasks",
            json={"title": "Design pass", "description": "Decide.", "kind": "design"},
        )
        assert created.status_code in (200, 201), created.text
        task_id = created.json()["id"]
        assert created.json()["kind"] == "design"

        other = http.post(
            "/api/projects/solo/tasks", json={"title": "Build", "description": "Build."}
        ).json()
        assert other.get("kind") is None

        read = http.get(f"/api/projects/solo/tasks/{task_id}").json()
        assert read["kind"] == "design"

        rows = http.get("/api/projects/solo/tasks").json()
        assert {row["id"]: row.get("kind") for row in rows} == {
            task_id: "design",
            other["id"]: None,
        }
        design_only = http.get("/api/projects/solo/tasks", params={"kind": "design"}).json()
        assert [row["id"] for row in design_only] == [task_id]
        implementation = http.get(
            "/api/projects/solo/tasks", params={"kind": "implementation"}
        ).json()
        assert [row["id"] for row in implementation] == [other["id"]]
        full = http.get("/api/projects/solo/tasks/full", params={"kind": "design"}).json()
        assert [row["id"] for row in full] == [task_id]

        patched = http.patch(f"/api/projects/solo/tasks/{other['id']}", json={"kind": "design"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["kind"] == "design"

    def test_the_detail_page_carries_each_related_tasks_kind(self, service) -> None:
        """task-593: a header says what a design is implemented by, from the read model."""
        _, manager, http = service
        design = manager.create_task(title="Design", description="Decide.", kind=TaskKind.DESIGN)
        build = manager.create_task(
            title="Build",
            description="Build.",
            dependencies=[Dependency(task=design.id, type=DependencyType.NEEDS)],
        )

        on_build = http.get(f"/api/projects/solo/tasks/{build.id}/detail").json()
        assert [(row["task_id"], row["kind"]) for row in on_build["needs"]] == [
            (design.id, "design")
        ]
        on_design = http.get(f"/api/projects/solo/tasks/{design.id}/detail").json()
        assert [(row["task_id"], row["kind"]) for row in on_design["blocks"]] == [(build.id, None)]

    def test_an_unknown_kind_is_refused(self, service) -> None:
        _, _, http = service
        response = http.post(
            "/api/projects/solo/tasks",
            json={"title": "x", "description": "y", "kind": "research"},
        )
        # This app answers request validation with 400, not FastAPI's default 422.
        assert response.status_code == 400
        assert "kind" in response.text


class TestMcp:
    def test_create_ready_update_and_read(self, service) -> None:
        registry, _, _ = service
        created = call(
            registry,
            "task_create_ready",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Design pass",
                "summary": "Decide the thing.",
                "description": "Decide.",
                "kind": "design",
            },
        )
        assert created["task"]["kind"] == "design"

        draft = call(
            registry,
            "task_create_draft",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Build",
                "summary": "Build the thing.",
                "description": "Build.",
            },
        )
        assert "kind" not in draft["task"]

        task_id = draft["task"]["id"]
        updated = call(
            registry,
            "task_update_content",
            {
                "project_id": "solo",
                "task_id": task_id,
                "actor": "bot",
                "operation_id": op(),
                "expected_revision": draft["task"]["updated"],
                "patch": {"kind": "design"},
            },
        )
        assert updated["task"]["kind"] == "design"
        # Logged like any content edit, so a backfill leaves a trail per task.
        assert updated["task"]["log"][-1]["data"]["fields"] == ["kind"]

        read = call(registry, "task_get", {"project_id": "solo", "task_id": task_id})
        assert read["task"]["kind"] == "design"
