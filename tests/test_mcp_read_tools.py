"""Contract and behaviour coverage for the five read-only MCP tools.

Driven against the real FastAPI application over the real read routes. The tools are
thin, so what is worth testing is not the shaping code in isolation but whether the
shapes survive a round trip through the service: whether a broken file stays visible,
whether a limit reports truncation, and whether two projects that share a task id stay
apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Tuple

import anyio
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import types

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.mcp import read_tools, summaries
from agentjobs.mcp.errors import ErrorCode, ToolError
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Dependency,
    DependencyType,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

READ_NAMES = [
    "projects_list",
    "tasks_list",
    "task_get",
    "tasks_search",
    "task_next",
]
"""The read inventory. Named rather than counted: the registry also carries the
mutation tools, and a count would not notice a rename."""


def read_declarations(registry):
    """Only the read tools, so mutation tools do not fail read-only assertions."""
    return [item for item in registry.declarations() if item.name in set(READ_NAMES)]


SHARED_ID = "task-001-shared-id"
ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
]


def _now() -> datetime:
    return datetime(2026, 8, 10, tzinfo=timezone.utc)


def _task(task_id: str, title: str, **kwargs: Any) -> Task:
    fields: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "created": _now(),
        "updated": _now(),
        "lifecycle": Lifecycle.READY,
        "ball": Ball.AGENT,
        "ball_reason": BallReason.AVAILABLE,
        "ball_prompt": "Pick this up.",
        "priority": Priority.HIGH,
        "category": "infrastructure",
        "spec": Spec(summary=f"Summary of {title}", description=f"Description of {title}"),
    }
    fields.update(kwargs)
    return Task(**fields)


def build_project(root: Path, name: str, tasks: list) -> TaskStorage:
    """Create a project with an actor vocabulary and the supplied tasks."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": name,
                "tasks_directory": "tasks",
                "actors": ACTORS,
                "default_user": "Ada",
            }
        ),
        encoding="utf-8",
    )
    storage = TaskStorage(root / "tasks")
    for task in tasks:
        storage.save_task(task)
    return storage


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> Iterator[Tuple[ToolRegistry, TaskClient, Path]]:
    """Two projects sharing a task id, one of them holding a corrupt file."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    parent = _task("task-900-umbrella", "Umbrella")
    child = _task("task-901-child", "Child", parent="task-900-umbrella")
    blocked = _task(
        "task-902-blocked",
        "Blocked",
        dependencies=[Dependency(task="task-901-child", type=DependencyType.NEEDS)],
    )
    resumable = _task(
        SHARED_ID,
        "Alpha task",
        lifecycle=Lifecycle.ACTIVE,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Review the branch and approve the merge.",
        # An active task is a claimed task; the model refuses one without an owner.
        assignment=Assignment(owner="bot"),
        log=[
            LogEntry(
                id=1,
                ts=_now(),
                actor="bot",
                type=LogEntryType.DECISION,
                body="Chose YAML over a database.",
            )
        ],
    )
    build_project(tmp_path / "alpha", "Alpha", [parent, child, blocked, resumable])
    build_project(tmp_path / "beta", "Beta", [_task(SHARED_ID, "Beta task")])

    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / "alpha", project_id="alpha")
    registry.add(tmp_path / "beta", project_id="beta")

    with TestClient(app) as http:
        client = TaskClient("http://testserver", client=http)
        yield build_registry(client), client, tmp_path

    reset_dependency_cache()


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke one tool and return its structured payload, validated against its schema."""

    async def run():
        definition = registry.get(name)
        result = await definition.handler(arguments)
        assert not isinstance(result, types.CallToolResult), "tool reported an error"
        content, structured = result
        # Every tool must give a text-only client something readable too.
        assert content and content[0].text.strip()
        jsonschema.validate(instance=structured, schema=definition.output_schema)
        return structured

    return anyio.run(run)


def break_a_task_file(root: Path) -> str:
    """Write a file that exists and will not load, returning its filename."""
    path = root / "alpha" / "tasks" / "task-999-corrupt.yaml"
    path.write_text("id: task-999-corrupt\nlifecycle: active\n", encoding="utf-8")
    return path.name


# ---------------------------------------------------------------------------
# ac-1: the published contract
# ---------------------------------------------------------------------------
class TestPublishedContract:
    def test_exactly_the_five_accepted_read_tools_are_published(self, service):
        registry, _, _ = service

        assert [name for name in registry.names if name in set(READ_NAMES)] == READ_NAMES

    def test_the_read_tools_come_first_in_the_inventory(self, service):
        """A client renders the list in order; looking should precede writing."""
        registry, _, _ = service

        assert registry.names[: len(READ_NAMES)] == READ_NAMES

    def test_every_tool_declares_both_schemas_and_a_description(self, service):
        registry, _, _ = service

        for declared in read_declarations(registry):
            assert declared.inputSchema["type"] == "object"
            assert declared.outputSchema is not None
            assert declared.description and len(declared.description) > 40

    def test_every_read_tool_is_annotated_read_only_and_closed_world(self, service):
        registry, _, _ = service

        for declared in read_declarations(registry):
            annotations = declared.annotations
            assert annotations is not None
            assert annotations.readOnlyHint is True
            assert annotations.destructiveHint is False
            assert annotations.openWorldHint is False

    def test_only_projects_list_may_omit_a_project_id(self, service):
        registry, _, _ = service

        for declared in read_declarations(registry):
            required = declared.inputSchema.get("required", [])
            if declared.name == "projects_list":
                assert required == []
            else:
                assert "project_id" in required

    def test_input_schemas_are_closed(self, service):
        """A silently ignored argument is worse than a rejected one."""
        registry, _, _ = service

        for declared in read_declarations(registry):
            assert declared.inputSchema["additionalProperties"] is False

    def test_no_read_tool_offers_a_claim_shortcut(self, service):
        """The read layer never mutates, not even as a convenience."""
        registry, _, _ = service

        for declared in read_declarations(registry):
            blob = (declared.description or "").lower()
            assert "claim-on" not in blob
            for schema in (declared.inputSchema, declared.outputSchema or {}):
                assert "claim" not in (schema.get("properties") or {})


# ---------------------------------------------------------------------------
# projects_list
# ---------------------------------------------------------------------------
class TestProjectsList:
    def test_lists_projects_with_their_actor_vocabularies(self, service):
        registry, _, _ = service

        payload = call(registry, "projects_list", {})

        projects = {item["id"]: item for item in payload["projects"]}
        assert set(projects) == {"alpha", "beta"}
        assert [actor["id"] for actor in projects["alpha"]["actors"]] == ["Ada", "bot"]
        assert projects["alpha"]["default_user"] == "Ada"


# ---------------------------------------------------------------------------
# tasks_list -- ac-3
# ---------------------------------------------------------------------------
class TestTasksList:
    def test_lists_one_projects_tasks_as_summaries(self, service):
        registry, _, _ = service

        payload = call(registry, "tasks_list", {"project_id": "alpha"})

        ids = {row["id"] for row in payload["tasks"]}
        assert SHARED_ID in ids and "task-901-child" in ids
        assert all(row["project_id"] == "alpha" for row in payload["tasks"])

    def test_a_summary_carries_the_state_needed_to_choose(self, service):
        registry, _, _ = service

        rows = {
            row["id"]: row for row in call(registry, "tasks_list", {"project_id": "alpha"})["tasks"]
        }

        row = rows[SHARED_ID]
        assert row["lifecycle"] == "active"
        assert row["ball"] == "human"
        assert row["ball_reason"] == "review"
        assert row["ball_prompt"] == "Review the branch and approve the merge."
        assert row["display_status"]

    def test_filters_apply(self, service):
        registry, _, _ = service

        payload = call(registry, "tasks_list", {"project_id": "alpha", "lifecycle": "active"})

        assert [row["id"] for row in payload["tasks"]] == [SHARED_ID]

    def test_children_of_an_umbrella_are_addressable(self, service):
        registry, _, _ = service

        payload = call(
            registry, "tasks_list", {"project_id": "alpha", "parent": "task-900-umbrella"}
        )

        assert [row["id"] for row in payload["tasks"]] == ["task-901-child"]

    def test_a_broken_file_is_reported_beside_the_valid_tasks(self, service):
        registry, _, root = service
        filename = break_a_task_file(root)

        payload = call(registry, "tasks_list", {"project_id": "alpha"})

        assert payload["tasks"], "valid tasks must still be listed"
        assert [item["filename"] for item in payload["broken"]] == [filename]
        assert payload["broken"][0]["reason"]

    def test_a_limit_truncates_and_says_so(self, service):
        registry, _, _ = service

        payload = call(registry, "tasks_list", {"project_id": "alpha", "limit": 2})

        assert len(payload["tasks"]) == 2
        assert payload["truncated"] is True

    def test_an_untruncated_result_says_so_too(self, service):
        registry, _, _ = service

        payload = call(registry, "tasks_list", {"project_id": "alpha", "limit": 200})

        assert payload["truncated"] is False

    @pytest.mark.parametrize("limit", [0, -1, 201, 1000])
    def test_a_limit_outside_the_accepted_range_is_refused(self, service, limit):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("tasks_list").handler({"project_id": "alpha", "limit": limit})
            return caught.value

        assert anyio.run(run).code is ErrorCode.INVALID_INPUT

    def test_a_non_integer_limit_is_refused(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError):
                await registry.get("tasks_list").handler({"project_id": "alpha", "limit": "10"})

        anyio.run(run)


# ---------------------------------------------------------------------------
# task_get -- ac-2
# ---------------------------------------------------------------------------
class TestTaskGet:
    def test_returns_the_complete_stored_record(self, service):
        registry, _, _ = service

        payload = call(registry, "task_get", {"project_id": "alpha", "task_id": SHARED_ID})

        task = payload["task"]
        assert task["id"] == SHARED_ID
        assert task["spec"]["description"] == "Description of Alpha task"
        assert task["ball_prompt"] == "Review the branch and approve the merge."
        assert task["log"][0]["type"] == "decision"
        assert task["log"][0]["body"] == "Chose YAML over a database."

    def test_computed_state_is_returned_apart_from_the_stored_document(self, service):
        """Derived values must not look like fields a caller could set."""
        registry, _, _ = service

        payload = call(registry, "task_get", {"project_id": "alpha", "task_id": SHARED_ID})

        assert "display_status" not in payload["task"]
        assert "actionable" not in payload["task"]
        assert set(payload["dependency_facts"]) >= {
            "actionable",
            "unmet_needs",
            "needs_cycles",
            "unblocks_count",
        }

    def test_dependency_facts_report_a_real_block(self, service):
        registry, _, _ = service

        payload = call(registry, "task_get", {"project_id": "alpha", "task_id": "task-902-blocked"})

        assert payload["dependency_facts"]["actionable"] is False
        assert payload["dependency_facts"]["unmet_needs"]

    def test_children_are_returned_as_summaries(self, service):
        registry, _, _ = service

        payload = call(
            registry, "task_get", {"project_id": "alpha", "task_id": "task-900-umbrella"}
        )

        assert [child["id"] for child in payload["subtasks"]] == ["task-901-child"]

    def test_a_missing_task_is_not_found(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("task_get").handler(
                    {"project_id": "alpha", "task_id": "task-404-nope"}
                )
            return caught.value

        error = anyio.run(run)
        assert error.code is ErrorCode.TASK_NOT_FOUND
        assert error.task_id == "task-404-nope"

    def test_a_broken_file_is_distinct_from_a_missing_task(self, service):
        """The constraint that made this program necessary in the first place."""
        registry, _, root = service
        break_a_task_file(root)

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("task_get").handler(
                    {"project_id": "alpha", "task_id": "task-999-corrupt"}
                )
            return caught.value

        error = anyio.run(run)
        assert error.code is ErrorCode.BROKEN_TASK
        assert error.code is not ErrorCode.TASK_NOT_FOUND
        assert "validate" in (error.suggested_action or "")


# ---------------------------------------------------------------------------
# tasks_search
# ---------------------------------------------------------------------------
class TestTasksSearch:
    def test_finds_by_substring(self, service):
        registry, _, _ = service

        payload = call(registry, "tasks_search", {"project_id": "alpha", "query": "Umbrella"})

        assert [row["id"] for row in payload["tasks"]] == ["task-900-umbrella"]

    def test_an_empty_query_is_refused(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("tasks_search").handler({"project_id": "alpha", "query": "  "})
            return caught.value

        assert anyio.run(run).code is ErrorCode.INVALID_INPUT

    def test_broken_files_are_reported_with_results(self, service):
        registry, _, root = service
        break_a_task_file(root)

        payload = call(registry, "tasks_search", {"project_id": "alpha", "query": "task"})

        assert payload["broken"]


# ---------------------------------------------------------------------------
# task_next -- ac-4
# ---------------------------------------------------------------------------
class TestTaskNext:
    def test_returns_a_claimable_task_without_claiming_it(self, service):
        registry, client, _ = service

        payload = call(registry, "task_next", {"project_id": "alpha", "actor": "bot"})

        assert payload["task"] is not None
        chosen = payload["task"]["id"]
        assert "NOT claimed" in payload["explanation"]

        after = client.for_project("alpha").get_task(chosen)
        assert after.lifecycle is Lifecycle.READY
        assert after.assignment.owner is None

    def test_an_unknown_actor_is_refused(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("task_next").handler({"project_id": "alpha", "actor": "gpt"})
            return caught.value

        assert anyio.run(run).code is ErrorCode.UNKNOWN_ACTOR

    def test_an_empty_project_explains_that_it_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()
        build_project(tmp_path / "empty", "Empty", [])
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "empty", project_id="empty")

        with TestClient(app) as http:
            registry = build_registry(TaskClient("http://testserver", client=http))
            payload = call(registry, "task_next", {"project_id": "empty", "actor": "bot"})

        reset_dependency_cache()
        assert payload["task"] is None
        assert "no tasks at all" in payload["explanation"]

    def test_fully_blocked_work_is_distinguished_from_an_empty_backlog(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()
        blocked = _task(
            "task-910-blocked",
            "Blocked",
            dependencies=[Dependency(task="task-911-absent", type=DependencyType.NEEDS)],
        )
        build_project(tmp_path / "stuck", "Stuck", [blocked])
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "stuck", project_id="stuck")

        with TestClient(app) as http:
            registry = build_registry(TaskClient("http://testserver", client=http))
            payload = call(registry, "task_next", {"project_id": "stuck", "actor": "bot"})

        reset_dependency_cache()
        assert payload["task"] is None
        assert "unmet dependencies" in payload["explanation"]
        assert "task-910-blocked" in payload["explanation"]

    def test_unreadable_files_are_named_in_the_explanation(self, tmp_path, monkeypatch):
        """Otherwise "nothing to claim" quietly means "I could not read the backlog"."""
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()
        build_project(tmp_path / "corrupt", "Corrupt", [])
        (tmp_path / "corrupt" / "tasks" / "task-999-corrupt.yaml").write_text(
            "id: task-999-corrupt\nlifecycle: active\n", encoding="utf-8"
        )
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "corrupt", project_id="corrupt")

        with TestClient(app) as http:
            registry = build_registry(TaskClient("http://testserver", client=http))
            payload = call(registry, "task_next", {"project_id": "corrupt", "actor": "bot"})

        reset_dependency_cache()
        assert payload["task"] is None
        assert "could not be read" in payload["explanation"]
        assert "task-999-corrupt.yaml" in payload["explanation"]


# ---------------------------------------------------------------------------
# ac-5: project isolation
# ---------------------------------------------------------------------------
class TestProjectIsolation:
    def test_colliding_ids_return_only_the_addressed_project(self, service):
        registry, _, _ = service

        alpha = call(registry, "task_get", {"project_id": "alpha", "task_id": SHARED_ID})
        beta = call(registry, "task_get", {"project_id": "beta", "task_id": SHARED_ID})

        assert alpha["task"]["title"] == "Alpha task"
        assert beta["task"]["title"] == "Beta task"
        assert alpha["project_id"] == "alpha" and beta["project_id"] == "beta"

    def test_alternating_calls_do_not_leak(self, service):
        registry, _, _ = service

        seen = []
        for _ in range(3):
            for project_id in ("alpha", "beta"):
                payload = call(
                    registry, "task_get", {"project_id": project_id, "task_id": SHARED_ID}
                )
                seen.append(payload["task"]["title"])

        assert seen == ["Alpha task", "Beta task"] * 3

    def test_a_task_absent_from_the_addressed_project_is_not_found(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("task_get").handler(
                    {"project_id": "beta", "task_id": "task-900-umbrella"}
                )
            return caught.value

        assert anyio.run(run).code is ErrorCode.TASK_NOT_FOUND

    def test_an_unknown_project_names_the_valid_ids(self, service):
        registry, _, _ = service

        async def run():
            with pytest.raises(ToolError) as caught:
                await registry.get("tasks_list").handler({"project_id": "gamma"})
            return caught.value

        error = anyio.run(run)
        assert error.code is ErrorCode.UNKNOWN_PROJECT
        assert "alpha" in error.message and "beta" in error.message

    def test_a_missing_project_id_is_refused_on_every_task_tool(self, service):
        registry, _, _ = service

        async def run():
            for name in ("tasks_list", "task_get", "tasks_search", "task_next"):
                with pytest.raises(ToolError) as caught:
                    await registry.get(name).handler({})
                assert caught.value.code is ErrorCode.INVALID_INPUT

        anyio.run(run)


# ---------------------------------------------------------------------------
# Shaping helpers
# ---------------------------------------------------------------------------
class TestSummaries:
    def test_a_summary_stamps_the_project_on_every_row(self):
        row = summaries.task_summary({"id": "task-1", "title": "T"}, project_id="alpha")

        assert row["project_id"] == "alpha"

    def test_an_uncomputed_child_count_is_null_not_zero(self):
        """Regression for task-180: the absence of a count is not the number zero.

        A read surface that never computed `open_children_count` used to have the
        summary layer fill the gap with 0, which reads as "this parent has no open
        children". That is a different claim, it is queryable, and it was wrong.
        """
        row = summaries.task_summary({"id": "task-1", "title": "T"}, project_id="alpha")

        assert row["open_children_count"] is None
        assert summaries.dependency_facts({})["open_children_count"] is None

    def test_a_computed_zero_child_count_survives_as_zero(self):
        """And a real 0 must not be turned into a null on the way through."""
        record = {"id": "task-1", "title": "T", "open_children_count": 0}

        assert summaries.task_summary(record, project_id="alpha")["open_children_count"] == 0
        assert summaries.dependency_facts(record)["open_children_count"] == 0

    @pytest.mark.parametrize(
        "count,plural,expected",
        [
            (1, None, "1 task."),
            (2, None, "2 tasks."),
            (1, "matches", "1 match."),
            (3, "matches", "3 matches."),
        ],
    )
    def test_the_summary_line_pluralises_correctly(self, count, plural, expected):
        """Deriving the plural produced "3 matchs" in real tool output."""
        noun = "task" if plural is None else "match"

        assert summaries.summary_line(count, noun, plural=plural) == expected

    def test_the_summary_line_names_unreadable_files(self):
        line = summaries.summary_line(2, "task", truncated=True, broken=1)

        assert "more available" in line
        assert "1 unreadable task file" in line

    def test_limited_reports_truncation_only_when_it_cuts(self):
        assert summaries.limited([1, 2, 3], 5) == ([1, 2, 3], False)
        assert summaries.limited([1, 2, 3], 2) == ([1, 2], True)
        assert summaries.limited([1, 2], 2) == ([1, 2], False)

    def test_the_default_and_bounds_match_the_accepted_design(self):
        assert (read_tools.MIN_LIMIT, read_tools.DEFAULT_LIMIT, read_tools.MAX_LIMIT) == (
            1,
            100,
            200,
        )
