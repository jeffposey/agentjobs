"""Contract and rejection coverage for the nine mutation MCP tools.

Driven against the real FastAPI application, so every success goes all the way to a
YAML file and back and every refusal is the one the authoritative manager actually
produces. Mocking TaskClient here would test the shaping code against my own idea of
what the manager does, which is the assumption most likely to be wrong.
"""

from __future__ import annotations

import uuid
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
from agentjobs.manager import TaskManager
from agentjobs.mcp import mutation_tools
from agentjobs.mcp.errors import ErrorCode, ToolError
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import Lifecycle
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
    {"name": "other", "kind": "agent", "display_name": "Other Bot"},
]

MUTATION_NAMES = [
    "task_create_draft",
    "task_create_ready",
    "task_promote",
    "task_claim",
    "task_release",
    "task_handoff",
    "task_close",
    "task_log_append",
    "task_update_content",
]


def op() -> str:
    """A fresh operation id."""
    return str(uuid.uuid4())


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> Iterator[Tuple[ToolRegistry, TaskManager, TaskClient]]:
    """The real app over one registered project with an actor vocabulary."""
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
    manager = TaskManager(TaskStorage(root / "tasks"))

    with TestClient(app) as http:
        client = TaskClient("http://testserver", client=http)
        yield build_registry(client), manager, client

    reset_dependency_cache()


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke a tool, validating its arguments and result against its own schemas."""

    async def run():
        definition = registry.get(name)
        # The SDK validates arguments against inputSchema before dispatch, so the test
        # does the same. Otherwise a schema could forbid something the handler still
        # cheerfully accepts, and the suite would never notice.
        jsonschema.validate(instance=dict(arguments), schema=definition.input_schema)
        result = await definition.handler(arguments)
        assert not isinstance(result, types.CallToolResult), "tool reported an error"
        content, structured = result
        assert content and content[0].text.strip()
        jsonschema.validate(instance=structured, schema=definition.output_schema)
        return structured

    return anyio.run(run)


def refuse(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> ToolError:
    """Invoke a tool expecting the handler to refuse, returning the error."""

    async def run():
        with pytest.raises(ToolError) as caught:
            await registry.get(name).handler(arguments)
        return caught.value

    return anyio.run(run)


def schema_rejects(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> bool:
    """Whether the tool's own input schema refuses these arguments."""
    try:
        jsonschema.validate(instance=dict(arguments), schema=registry.get(name).input_schema)
    except jsonschema.ValidationError:
        return True
    return False


def base(**overrides: Any) -> Dict[str, Any]:
    """Arguments common to every state verb."""
    arguments = {
        "project_id": "solo",
        "task_id": "task-001-work",
        "actor": "bot",
        "operation_id": op(),
    }
    arguments.update(overrides)
    return arguments


def ready_task(manager: TaskManager, task_id: str = "task-001-work"):
    """A ready task with no dependencies."""
    return manager.create_task(
        id=task_id,
        title="Work",
        description="Do the thing.",
        category="general",
        lifecycle=Lifecycle.READY,
    )


# ---------------------------------------------------------------------------
# ac-1 and ac-4: the published surface, and what is absent from it
# ---------------------------------------------------------------------------
class TestPublishedSurface:
    def test_exactly_the_eight_accepted_mutation_tools_are_published(self, service):
        registry, _, _ = service

        published = [name for name in registry.names if name in set(MUTATION_NAMES)]
        assert published == MUTATION_NAMES

    def test_every_mutation_requires_project_actor_and_operation_id(self, service):
        registry, _, _ = service

        for name in MUTATION_NAMES:
            required = registry.get(name).input_schema["required"]
            assert {"project_id", "actor", "operation_id"} <= set(required), name

    def test_the_verbs_that_act_on_read_content_require_a_revision(self, service):
        registry, _, _ = service

        for name in ("task_handoff", "task_close", "task_update_content"):
            assert "expected_revision" in registry.get(name).input_schema["required"], name

    def test_an_append_does_not_require_a_revision(self, service):
        """Two agents writing independent progress entries must not conflict."""
        registry, _, _ = service

        assert "expected_revision" not in registry.get("task_log_append").input_schema["required"]

    def test_mutations_are_annotated_non_read_only(self, service):
        registry, _, _ = service

        for name in MUTATION_NAMES:
            annotations = registry.get(name).to_tool().annotations
            assert annotations is not None
            assert annotations.readOnlyHint is False, name
            assert annotations.idempotentHint is True, name

    def test_only_close_is_annotated_destructive(self, service):
        registry, _, _ = service

        destructive = [
            name
            for name in MUTATION_NAMES
            if registry.get(name).to_tool().annotations.destructiveHint
        ]
        assert destructive == ["task_close"]

    def test_no_generic_state_yaml_batch_or_create_and_claim_tool_exists(self, service):
        """ac-4: what is absent is as much the contract as what is present."""
        registry, _, _ = service

        forbidden = (
            "set_status",
            "set_lifecycle",
            "set_ball",
            "set_outcome",
            "save_yaml",
            "write_yaml",
            "patch_task",
            "update_task",
            "batch",
            "create_and_claim",
            "transition",
        )
        for name in registry.names:
            assert not any(bad in name for bad in forbidden), name

    def test_no_top_level_argument_is_a_state_axis(self, service):
        """A field absent from every schema cannot be set by any argument."""
        registry, _, _ = service
        axes = {"lifecycle", "ball", "ball_reason", "ball_prompt", "outcome", "archived", "log"}

        for name in MUTATION_NAMES:
            properties = set(registry.get(name).input_schema["properties"])
            # `outcome` is close's own argument: it is data about how work ended, not
            # a lifecycle setter, and close is the only verb that may set it.
            allowed = {"outcome"} if name == "task_close" else set()
            assert not ((axes & properties) - allowed), name

    def test_no_nested_object_smuggles_a_state_axis_back_in(self, service):
        """The patch and target objects are closed, so this checks they stay closed."""
        registry, _, _ = service
        axes = {"lifecycle", "ball_reason", "ball_prompt", "outcome", "archived", "log"}

        for name in MUTATION_NAMES:
            for key, schema in registry.get(name).input_schema["properties"].items():
                nested = schema.get("properties")
                if not isinstance(nested, dict):
                    continue
                assert not (axes & set(nested)), f"{name}.{key}"

    def test_the_content_patch_exposes_only_authoring_fields(self, service):
        registry, _, _ = service

        patch = registry.get("task_update_content").input_schema["properties"]["patch"]

        assert set(patch["properties"]) == {
            "title",
            "priority",
            "category",
            "effort",
            "tags",
            "parent",
            "spec",
            "acceptance",
            "deliverables",
            "dependencies",
            "links",
            "branches",
        }
        assert patch["additionalProperties"] is False

    def test_a_create_schema_cannot_set_a_starting_state(self, service):
        """Draft or ready is decided by which tool you call, never by an argument."""
        registry, _, _ = service

        for name in ("task_create_draft", "task_create_ready"):
            properties = registry.get(name).input_schema["properties"]
            assert "lifecycle" not in properties
            assert "ball" not in properties
            assert "log" not in properties
            assert "branches" not in properties


# ---------------------------------------------------------------------------
# ac-2: the happy paths, end to end
# ---------------------------------------------------------------------------
class TestCreation:
    def test_a_draft_is_born_with_the_ball_on_a_human(self, service):
        registry, _, _ = service

        payload = call(
            registry,
            "task_create_draft",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Draft it",
                "summary": "A short orientation.",
                "description": "The working spec.",
            },
        )

        task = payload["task"]
        assert task["lifecycle"] == "draft"
        assert task["ball"] == "human"
        assert task["ball_reason"] == "spec"

    def test_a_ready_task_is_born_claimable_and_unclaimed(self, service):
        registry, _, _ = service

        payload = call(
            registry,
            "task_create_ready",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Ready it",
                "summary": "A short orientation.",
                "description": "The working spec.",
            },
        )

        task = payload["task"]
        assert task["lifecycle"] == "ready"
        assert task["ball"] == "agent"
        assert task["ball_reason"] == "available"
        assert task.get("assignment", {}).get("owner") is None

    def test_the_full_spec_survives_creation(self, service):
        registry, _, _ = service

        payload = call(
            registry,
            "task_create_ready",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "id": "task-500-rich",
                "title": "Rich",
                "summary": "Orientation.",
                "description": "Spec.",
                "intent": "Why it exists.",
                "constraints": "Hard rules.",
                "out_of_scope": "Not this.",
                "context": [{"path": "src/x.py", "why": "It is the thing."}],
                "priority": "high",
                "category": "infrastructure",
                "tags": ["a", "b"],
            },
        )

        spec = payload["task"]["spec"]
        assert spec["intent"] == "Why it exists."
        assert spec["context"][0]["path"] == "src/x.py"
        assert payload["task"]["priority"] == "high"
        assert payload["task"]["tags"] == ["a", "b"]

    def test_a_retried_create_resolves_to_the_same_task(self, service):
        registry, manager, _ = service
        arguments = {
            "project_id": "solo",
            "actor": "bot",
            "operation_id": op(),
            "title": "Once",
            "summary": "s",
            "description": "d",
        }

        first = call(registry, "task_create_ready", arguments)
        second = call(registry, "task_create_ready", arguments)

        assert first["task"]["id"] == second["task"]["id"]
        assert len(manager.list_tasks()) == 1


class TestStateVerbs:
    def test_claim_takes_ownership(self, service):
        registry, manager, _ = service
        ready_task(manager)

        payload = call(registry, "task_claim", base())

        assert payload["task"]["lifecycle"] == "active"
        assert payload["task"]["assignment"]["owner"] == "bot"
        assert payload["replayed"] is False

    def test_promote_is_the_only_exit_from_draft(self, service):
        registry, manager, _ = service
        manager.create_task(
            id="task-001-work",
            title="Work",
            description="Do the thing.",
            category="general",
            lifecycle=Lifecycle.DRAFT,
        )

        drafted = call(registry, "task_get", {"project_id": "solo", "task_id": "task-001-work"})
        payload = call(
            registry,
            "task_promote",
            base(expected_revision=drafted["task"]["updated"], body="Spec is finished."),
        )

        assert payload["task"]["lifecycle"] == "ready"
        assert payload["task"]["ball"] == "agent"
        assert payload["task"]["ball_reason"] == "available"
        assert payload["replayed"] is False
        assert call(registry, "task_claim", base())["task"]["lifecycle"] == "active"

    def test_promote_refuses_a_stale_revision(self, service):
        registry, manager, _ = service
        manager.create_task(
            id="task-001-work",
            title="Work",
            description="Do the thing.",
            category="general",
            lifecycle=Lifecycle.DRAFT,
        )

        error = refuse(registry, "task_promote", base(expected_revision="2000-01-01T00:00:00Z"))
        assert error.code is ErrorCode.REVISION_CONFLICT

    def test_release_returns_the_task_to_the_pool(self, service):
        registry, manager, _ = service
        ready_task(manager)
        call(registry, "task_claim", base())

        payload = call(registry, "task_release", base(body="Not mine after all."))

        assert payload["task"]["lifecycle"] == "ready"
        assert payload["task"]["ball_reason"] == "available"

    @pytest.mark.parametrize(
        "ball,reason",
        [
            ("agent", "work"),
            ("agent", "revise"),
            ("human", "spec"),
            ("human", "review"),
            ("human", "decision"),
            ("human", "approval"),
            ("human", "input"),
            ("external", "dependency"),
            ("external", "service"),
        ],
    )
    def test_every_valid_handoff_family_works(self, service, ball, reason):
        registry, manager, _ = service
        task = ready_task(manager)
        call(registry, "task_claim", base())
        current = manager.get_task(task.id).updated.isoformat()

        payload = call(
            registry,
            "task_handoff",
            base(
                expected_revision=current,
                target={"ball": ball, "reason": reason, "prompt": "Do the next thing."},
            ),
        )

        assert payload["task"]["ball"] == ball
        assert payload["task"]["ball_reason"] == reason
        assert payload["task"]["ball_prompt"] == "Do the next thing."

    def test_close_ends_the_task_with_its_outcome(self, service):
        registry, manager, _ = service
        task = ready_task(manager)
        call(registry, "task_claim", base())
        current = manager.get_task(task.id).updated.isoformat()

        payload = call(registry, "task_close", base(expected_revision=current, outcome="completed"))

        assert payload["task"]["lifecycle"] == "closed"
        assert payload["task"]["outcome"] == "completed"
        assert payload["task"].get("ball") is None

    def test_log_append_records_an_authored_entry(self, service):
        registry, manager, _ = service
        ready_task(manager)

        payload = call(
            registry,
            "task_log_append",
            base(type="decision", body="Chose YAML.", data={"why": "git"}),
        )

        entry = payload["task"]["log"][-1]
        assert entry["type"] == "decision"
        assert entry["body"] == "Chose YAML."
        assert entry["actor"] == "bot"

    def test_content_update_edits_authoring_fields(self, service):
        registry, manager, _ = service
        task = ready_task(manager)

        payload = call(
            registry,
            "task_update_content",
            base(
                expected_revision=task.updated.isoformat(),
                patch={"title": "Renamed", "priority": "critical", "tags": ["x"]},
            ),
        )

        assert payload["task"]["title"] == "Renamed"
        assert payload["task"]["priority"] == "critical"
        assert payload["task"]["tags"] == ["x"]

    def test_a_returned_task_is_the_reloaded_record(self, service):
        """A successful result means persisted, not "probably persisted"."""
        registry, manager, _ = service
        ready_task(manager)

        payload = call(registry, "task_claim", base())

        on_disk = manager.get_task("task-001-work")
        assert payload["task"]["updated"] == on_disk.updated.isoformat().replace("+00:00", "Z")

    def test_a_result_never_carries_computed_state_as_a_field(self, service):
        registry, manager, _ = service
        ready_task(manager)

        payload = call(registry, "task_claim", base())

        assert "display_status" not in payload["task"]


# ---------------------------------------------------------------------------
# ac-3: refusals
# ---------------------------------------------------------------------------
class TestSchemaRefusals:
    def test_an_invalid_holder_reason_pair_does_not_validate(self, service):
        """human/work reads fine as two fields; the union is what makes it impossible."""
        registry, _, _ = service

        assert schema_rejects(
            registry,
            "task_handoff",
            base(
                expected_revision="2026-08-10T00:00:00Z",
                target={"ball": "human", "reason": "work", "prompt": "x"},
            ),
        )

    def test_agent_available_is_not_a_handoff_target(self, service):
        """Returning work to the pool is task_release, not a handoff alias."""
        registry, _, _ = service

        assert schema_rejects(
            registry,
            "task_handoff",
            base(
                expected_revision="2026-08-10T00:00:00Z",
                target={"ball": "agent", "reason": "available", "prompt": "x"},
            ),
        )

    def test_a_handoff_without_its_ask_does_not_validate(self, service):
        registry, _, _ = service

        assert schema_rejects(
            registry,
            "task_handoff",
            base(
                expected_revision="2026-08-10T00:00:00Z",
                target={"ball": "human", "reason": "review"},
            ),
        )

    @pytest.mark.parametrize("entry_type", ["transition", "handoff"])
    def test_a_manager_owned_log_type_cannot_be_authored(self, service, entry_type):
        registry, _, _ = service

        assert schema_rejects(registry, "task_log_append", base(type=entry_type, body="forged"))

    @pytest.mark.parametrize(
        "field", ["lifecycle", "ball", "ball_reason", "outcome", "log", "id", "created", "updated"]
    )
    def test_a_state_field_in_a_content_patch_does_not_validate(self, service, field):
        registry, _, _ = service

        assert schema_rejects(
            registry,
            "task_update_content",
            base(expected_revision="2026-08-10T00:00:00Z", patch={field: "anything"}),
        )

    def test_an_empty_patch_does_not_validate(self, service):
        registry, _, _ = service

        assert schema_rejects(
            registry, "task_update_content", base(expected_revision="x", patch={})
        )

    def test_a_close_without_an_outcome_does_not_validate(self, service):
        registry, _, _ = service

        assert schema_rejects(registry, "task_close", base(expected_revision="x"))

    def test_an_unknown_argument_does_not_validate(self, service):
        registry, _, _ = service

        assert schema_rejects(registry, "task_claim", base(surprise=True))


class TestDomainRefusals:
    def test_an_unknown_actor_is_refused_before_any_write(self, service):
        registry, manager, _ = service
        task = ready_task(manager)

        error = refuse(registry, "task_claim", base(actor="gpt"))

        assert error.code is ErrorCode.UNKNOWN_ACTOR
        assert manager.get_task(task.id).lifecycle is Lifecycle.READY

    def test_an_unknown_project_is_refused(self, service):
        registry, _, _ = service

        error = refuse(registry, "task_claim", base(project_id="nope"))

        assert error.code is ErrorCode.UNKNOWN_PROJECT

    def test_a_missing_task_is_not_found(self, service):
        registry, _, _ = service

        error = refuse(registry, "task_claim", base(task_id="task-404-absent"))

        assert error.code is ErrorCode.TASK_NOT_FOUND

    def test_a_second_claim_is_refused_with_the_manager_s_own_reason(self, service):
        registry, manager, _ = service
        ready_task(manager)
        call(registry, "task_claim", base())

        error = refuse(registry, "task_claim", base(actor="other"))

        assert error.code is ErrorCode.INVALID_TRANSITION
        assert "not available to claim" in error.message

    def test_a_stale_revision_is_refused_and_returns_the_current_task(self, service):
        registry, manager, _ = service
        task = ready_task(manager)
        stale = task.updated.isoformat()
        call(registry, "task_claim", base())

        error = refuse(
            registry,
            "task_handoff",
            base(
                expected_revision=stale,
                target={"ball": "human", "reason": "review", "prompt": "Look."},
            ),
        )

        assert error.code is ErrorCode.REVISION_CONFLICT
        assert error.current_task is not None
        assert manager.get_task(task.id).ball.value == "agent"

    def test_an_umbrella_with_open_children_is_dependency_blocked(self, service):
        registry, manager, _ = service
        parent = manager.create_task(
            id="task-900-umbrella",
            title="Umbrella",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
        )
        manager.create_task(
            id="task-901-child",
            title="Child",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
            parent=parent.id,
        )

        error = refuse(registry, "task_claim", base(task_id=parent.id))

        assert error.code is ErrorCode.DEPENDENCY_BLOCKED

    def test_reusing_an_operation_id_for_a_different_request_is_a_conflict(self, service):
        registry, manager, _ = service
        ready_task(manager)
        shared = op()
        call(registry, "task_log_append", base(operation_id=shared, body="one"))

        error = refuse(registry, "task_log_append", base(operation_id=shared, body="two"))

        assert error.code is ErrorCode.OPERATION_CONFLICT

    def test_a_refusal_carries_a_suggested_action(self, service):
        registry, _, _ = service

        error = refuse(registry, "task_claim", base(project_id="nope"))

        assert error.suggested_action


# ---------------------------------------------------------------------------
# ac-5: retries
# ---------------------------------------------------------------------------
class TestRetries:
    def test_a_retried_claim_reports_replayed_and_writes_nothing(self, service):
        registry, manager, _ = service
        ready_task(manager)
        arguments = base()

        first = call(registry, "task_claim", arguments)
        second = call(registry, "task_claim", arguments)

        assert first["replayed"] is False
        assert second["replayed"] is True
        assert second["task"]["updated"] == first["task"]["updated"]
        assert len(second["task"]["log"]) == len(first["task"]["log"])

    def test_a_retried_log_append_does_not_duplicate_the_entry(self, service):
        registry, manager, _ = service
        ready_task(manager)
        arguments = base(body="Only once.")

        call(registry, "task_log_append", arguments)
        payload = call(registry, "task_log_append", arguments)

        bodies = [entry["body"] for entry in payload["task"]["log"]]
        assert bodies.count("Only once.") == 1
        assert payload["replayed"] is True

    def test_a_retried_close_does_not_refuse_itself_as_already_closed(self, service):
        registry, manager, _ = service
        task = ready_task(manager)
        call(registry, "task_claim", base())
        arguments = base(
            expected_revision=manager.get_task(task.id).updated.isoformat(), outcome="completed"
        )

        call(registry, "task_close", arguments)
        payload = call(registry, "task_close", arguments)

        assert payload["replayed"] is True
        assert payload["task"]["lifecycle"] == "closed"


# ---------------------------------------------------------------------------
# Schema refusals, as the agent actually receives them
# ---------------------------------------------------------------------------
class TestSchemaRefusalsReachTheAgent:
    """A schema failure must arrive with a code, like every other refusal.

    The SDK's own validation returns text and no structuredContent, which makes it
    the one class of error an agent cannot branch on -- and the one it could most
    easily fix by itself. These go over the protocol rather than calling the handler,
    because the handler is not where the rejection happens.
    """

    def _over_the_protocol(self, registry, name, arguments):
        from mcp.shared.memory import create_connected_server_and_client_session

        from agentjobs.mcp.server import build_server

        async def run():
            async with create_connected_server_and_client_session(
                build_server(registry), raise_exceptions=False
            ) as session:
                return await session.call_tool(name, arguments)

        return anyio.run(run)

    def test_an_invalid_handoff_target_returns_a_structured_error(self, service):
        registry, manager, _ = service
        ready_task(manager)

        result = self._over_the_protocol(
            registry,
            "task_handoff",
            base(
                expected_revision="2026-01-01T00:00:00Z",
                target={"ball": "human", "reason": "work", "prompt": "x"},
            ),
        )

        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["code"] == "invalid_input"
        assert result.structuredContent["field_errors"]
        assert result.structuredContent["suggested_action"]

    def test_a_state_field_in_a_patch_returns_a_structured_error(self, service):
        registry, manager, _ = service
        ready_task(manager)

        result = self._over_the_protocol(
            registry,
            "task_update_content",
            base(expected_revision="2026-01-01T00:00:00Z", patch={"lifecycle": "active"}),
        )

        assert result.isError is True
        assert result.structuredContent["code"] == "invalid_input"

    def test_a_valid_call_still_succeeds_over_the_protocol(self, service):
        registry, manager, _ = service
        ready_task(manager)

        result = self._over_the_protocol(registry, "task_claim", base())

        assert result.isError is False
        assert result.structuredContent["task"]["lifecycle"] == "active"


# ---------------------------------------------------------------------------
# The architectural boundary
# ---------------------------------------------------------------------------
class TestBoundary:
    def test_the_mutation_tools_reach_the_service_only_through_taskclient(self):
        """No storage, no manager, no filesystem -- every write goes over HTTP.

        Parsed rather than grepped: the module docstring names ``save_yaml`` while
        explaining that no such tool exists, and a substring search would either trip
        on that or have to be loosened until it caught nothing.
        """
        import ast

        tree = ast.parse(Path(mutation_tools.__file__).read_text(encoding="utf-8"))
        forbidden_modules = {"yaml", "pathlib", "os", "io", "storage", "manager"}
        forbidden_names = {"TaskManager", "TaskStorage", "Path", "open"}

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    alias.name
                    for alias in node.names
                    if alias.name.split(".")[0] in forbidden_modules
                ]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").rsplit(".", 1)[-1] in forbidden_modules:
                    offenders.append(node.module or "")
                offenders += [alias.name for alias in node.names if alias.name in forbidden_names]
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                offenders.append(node.id)
        assert offenders == []
