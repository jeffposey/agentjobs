"""Contract and rejection coverage for the ten mutation MCP tools.

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

from agentjobs import capabilities
from agentjobs.api.authorization import denial_status
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient, TaskClientError
from agentjobs.dispatch.credentials import mint_run_credential
from agentjobs.dispatch.runner import RunDirectory, new_run_id
from agentjobs.manager import TaskManager
from agentjobs.mcp import mutation_tools
from agentjobs.mcp.errors import AUTHORIZATION_CODES, ERROR_SCHEMA, ErrorCode, ToolError
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.server import validate_arguments
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.principals import RUN_CREDENTIAL_HEADER, Problem
from agentjobs.projects import ProjectRegistry
from agentjobs.record_check import DEFAULT_BALL_PROMPT, LONG_SUMMARY, SUMMARY_WORD_CEILING
from support import task_store

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
    "task_authorize_dispatch",
    "task_update_content",
    "task_queue_move",
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
    manager = TaskManager(task_store(root / "tasks"))

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

    error: ToolError = anyio.run(run)
    return error


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
    def test_exactly_the_accepted_mutation_tools_are_published(self, service):
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

        for name in ("task_handoff", "task_close", "task_update_content", "task_queue_move"):
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
            # A request for a dispatch envelope, clamped by the project's machine-local
            # ceiling, so it grants nothing however writes it (task-308).
            "posture",
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

    def test_authorize_dispatch_names_the_human_beside_the_relaying_agent(self, service):
        """task-506. The one write here whose author and subject are different people.

        ``actor`` is the agent, because the agent is what typed it; the person is in the
        payload. A tool that let the agent put the person's id in ``actor`` instead would
        be the workaround this replaces, so the stored shape is what is asserted.
        """
        registry, manager, _ = service
        ready_task(manager)

        payload = call(
            registry,
            "task_authorize_dispatch",
            base(
                authorized_by="Ada",
                ask="File this and start it.",
                surface="an interactive chat session",
            ),
        )

        entry = payload["task"]["log"][-1]
        assert entry["type"] == "authorization"
        assert entry["actor"] == "bot"
        assert entry["data"]["authorized_by"] == "Ada"
        assert entry["body"] == "File this and start it."

    def test_authorize_dispatch_refuses_an_agent_as_the_authorizer(self, service):
        """It records who authorised; it does not widen who may.

        The entry is agent-authored by design, so the id it names having to be a human is
        the only thing between this tool and an agent authorising its own successor
        through the front door.
        """
        registry, manager, _ = service
        ready_task(manager)

        error = refuse(
            registry, "task_authorize_dispatch", base(authorized_by="other", ask="Start it.")
        )

        assert "agent" in str(error.message).lower()
        # Refused before anything is written, so no row of this type ever reaches the log.
        entries = manager.get_task("task-001-work").log
        assert [entry for entry in entries if entry.type is LogEntryType.AUTHORIZATION] == []

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

    @pytest.mark.parametrize("entry_type", ["transition", "handoff", "authorization"])
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

    def test_claiming_an_umbrella_hands_over_the_supervision_ask(self, service):
        """task-164. It used to refuse with DEPENDENCY_BLOCKED; now it says what to do.

        The claim succeeds because driving an epic is work someone has to own. What
        stops it being taken by accident is that the ask written back names the seat --
        supervise, one session per child -- rather than "execute the spec".
        """
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

        call(registry, "task_claim", base(task_id=parent.id))

        claimed = manager.get_task(parent.id)
        assert claimed.lifecycle is Lifecycle.ACTIVE
        assert "Supervise this epic" in (claimed.ball_prompt or "")
        assert "task-901-child" in (claimed.ball_prompt or "")

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
# task-426: the service's authorization refusals reach the agent under their own names
# ---------------------------------------------------------------------------
def service_denial_codes() -> set[str]:
    """Every refusal code the service can put in a 403/400 body, read from its source.

    Derived rather than listed: a code added to ``capabilities`` or to
    ``principals.Problem`` without a matching MCP member fails
    ``test_the_family_is_exactly_what_the_service_emits`` instead of reaching an agent
    as ``internal_error``, which is how task-332's codes went missing the first time.
    """
    exported = (getattr(capabilities, name) for name in capabilities.__all__)
    constants = {value for value in exported if isinstance(value, str)}
    return constants | {problem.value for problem in Problem}


def service_error(code: Any = None, *, status: int, **body: Any) -> ToolError:
    """Classify one client failure the way every mutation tool does."""
    if code is not None:
        body["code"] = code
    exc = TaskClientError(
        str(body.get("detail", f"HTTP {status}")), status_code=status, body=body or None
    )
    return mutation_tools._service_error(exc, project_id="solo", task_id="task-001-work")


class TestAuthorizationRefusalsReachTheAgent:
    def test_the_family_is_exactly_what_the_service_emits(self):
        assert {code.value for code in AUTHORIZATION_CODES} == service_denial_codes()

    @pytest.mark.parametrize("code", sorted(service_denial_codes()))
    def test_each_code_arrives_unchanged_with_something_to_do(self, code):
        """ac-1: the REST code is the MCP code, and it says what to do next."""
        error = service_error(
            code, status=denial_status(code), detail="Refused, with the service's reason."
        )

        payload = error.to_payload()
        assert payload["code"] == code
        assert payload["message"] == "Refused, with the service's reason."
        assert payload["suggested_action"]
        assert payload["retryable"] is False

    def test_a_service_supplied_action_is_not_overwritten(self):
        error = service_error("wrong_task", status=403, suggested_action="The service's own.")

        assert error.suggested_action == "The service's own."

    def test_a_code_this_build_does_not_know_is_internal_and_names_itself(self):
        """ac-4: the branch that used to be ``pragma: no cover``."""
        error = service_error("brand_new_refusal", status=403, detail="Something new.")

        assert error.code is ErrorCode.INTERNAL_ERROR
        assert "brand_new_refusal" in error.message
        assert "Something new." in error.message
        assert error.suggested_action

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_an_answered_5xx_without_a_code_is_retryable_unavailability(self, status):
        """ac-2: a restart that gets as far as answering is not an invalid transition."""
        error = service_error(status=status)

        assert error.code is ErrorCode.SERVICE_UNAVAILABLE
        assert error.retryable is True
        assert str(status) in error.message
        assert error.suggested_action

    def test_a_4xx_without_a_code_is_still_classified_as_before(self):
        assert service_error(status=409).code is ErrorCode.INVALID_TRANSITION
        assert service_error(status=404).code is ErrorCode.TASK_NOT_FOUND
        assert service_error(status=400).code is ErrorCode.INVALID_INPUT

    @pytest.mark.parametrize("code", sorted(service_denial_codes()))
    def test_the_published_error_schema_admits_the_family(self, code):
        """A strict client validating the refusal must not reject the refusal itself."""
        payload = service_error(code, status=403, detail="x").to_payload()

        jsonschema.validate(instance=payload, schema=ERROR_SCHEMA)

    def _credentialed(self, token: str) -> ToolRegistry:
        """The same app, reached by a client presenting this run credential."""
        http = TestClient(app, client=("127.0.0.1", 51000), headers={RUN_CREDENTIAL_HEADER: token})
        return build_registry(TaskClient("http://testserver", client=http))

    def test_a_run_may_write_to_another_task(self, service, tmp_path):
        """End to end: a real minted credential, over the real application.

        Until task-411 this refused ``wrong_task``; the owner lifted the scope.
        """
        _, manager, _ = service
        mine = ready_task(manager, "task-001-work")
        yours = ready_task(manager, "task-002-other")
        run_id = new_run_id()
        directory = RunDirectory.create(
            tmp_path / "home",
            run_id,
            {
                "run_id": run_id,
                "task_id": mine.id,
                "project_id": "solo",
                "mode": "session",
                "agent": "bot",
                "status": "running",
            },
        )
        token = mint_run_credential(directory.path, run_id)
        assert token, "the credential must mint, or this test proves nothing"

        call(self._credentialed(token), "task_claim", base(task_id=yours.id))

        assert manager.get_task(yours.id).lifecycle is Lifecycle.ACTIVE

    def test_a_run_whose_credential_does_not_verify_hears_so(self, service):
        """The audit's reproduction: an ended run's credential, every write refused."""
        _, manager, _ = service
        task = ready_task(manager)

        error = refuse(self._credentialed("not-a-real-credential"), "task_claim", base())

        assert error.code is ErrorCode.UNVERIFIED_RUN_CREDENTIAL
        assert error.suggested_action
        assert manager.get_task(task.id).lifecycle is Lifecycle.READY

    def test_the_refusal_is_what_an_mcp_client_receives(self, service):
        """Over the protocol, because ``structuredContent`` is what an agent branches on."""
        from mcp.shared.memory import create_connected_server_and_client_session

        from agentjobs.mcp.server import build_server

        _, manager, _ = service
        ready_task(manager)
        registry = self._credentialed("not-a-real-credential")

        async def run():
            async with create_connected_server_and_client_session(
                build_server(registry), raise_exceptions=False
            ) as session:
                return await session.call_tool("task_claim", base())

        result = anyio.run(run)

        assert result.isError is True
        assert result.structuredContent["code"] == "unverified_run_credential"
        assert result.structuredContent["retryable"] is False
        assert result.structuredContent["suggested_action"]


class TestSchemaRefusalsSayWhatIsValid:
    def test_an_operation_id_that_is_not_a_uuid_is_refused(self, service):
        registry, _, _ = service

        with pytest.raises(ToolError) as caught:
            validate_arguments(registry.get("task_claim"), base(operation_id="op-1"))

        assert caught.value.code is ErrorCode.INVALID_INPUT
        assert [item.path for item in caught.value.field_errors] == ["operation_id"]

    def test_a_uuid_operation_id_still_validates(self, service):
        registry, _, _ = service

        validate_arguments(registry.get("task_claim"), base())

    def test_an_invalid_handoff_pair_names_the_valid_pairs(self, service):
        registry, _, _ = service

        with pytest.raises(ToolError) as caught:
            validate_arguments(
                registry.get("task_handoff"),
                base(
                    expected_revision="2026-08-10T00:00:00Z",
                    target={"ball": "human", "reason": "work", "prompt": "x"},
                ),
            )

        message = caught.value.message
        for pair in ("agent/work", "human/review", "external/dependency", "external/service"):
            assert pair in message
        assert "agent/available" not in message
        assert "human/review" in caught.value.field_errors[0].message


# ---------------------------------------------------------------------------
# task-208: the order moves through a verb, and only through a verb
# ---------------------------------------------------------------------------
class TestQueueMove:
    """`task_queue_move` is the whole of the MCP surface for changing the order.

    The assertions worth having here are not that a move moves something -- the
    manager is tested for that -- but that the *tool* keeps the two properties the
    design leans on: no way to write a position number, and a retry that replays.
    """

    def _two_ready_tasks(self, manager: TaskManager):
        first = ready_task(manager, "task-001-work")
        second = ready_task(manager, "task-002-second")
        return first, second

    def test_it_moves_a_task_relative_to_a_neighbour(self, service):
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)
        assert first.queue_position < second.queue_position

        result = call(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=second.updated.isoformat(),
                placement={"where": "before", "neighbour": first.id},
                body="It blocks the release.",
            ),
        )

        assert result["task"]["queue_position"] < manager.get_task(first.id).queue_position

    def test_top_and_bottom_need_no_neighbour(self, service):
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)

        moved = call(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=second.updated.isoformat(),
                placement={"where": "top"},
            ),
        )

        assert moved["task"]["queue_position"] < manager.get_task(first.id).queue_position

    def test_the_decision_lands_in_the_log_as_a_queue_move_entry(self, service):
        """The number is recoverable from the file; the reasoning is only here."""
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)

        call(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=second.updated.isoformat(),
                placement={"where": "before", "neighbour": first.id},
                body="It blocks the release.",
            ),
        )

        entries = [
            item for item in manager.get_task(second.id).log if item.type.value == "queue_move"
        ]
        assert entries and entries[-1].body == "It blocks the release."

    def test_there_is_no_way_to_write_a_position_number(self, service):
        """The property the whole design rests on: a caller cannot choose a number.

        Two open tasks sharing one position is corruption the queue refuses to answer
        over, and a caller writing a number is choosing blind. Asserted against the
        published schema rather than the handler, because the schema is what an agent
        reads and what the SDK enforces before dispatch.
        """
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)

        schema = registry.get("task_queue_move").input_schema
        assert "queue_position" not in schema["properties"]
        assert "position" not in schema["properties"]
        for candidate in (
            {"where": "before", "neighbour": first.id, "position": 150},
            {"where": "top", "position": 1},
            {"position": 150},
        ):
            assert schema_rejects(
                registry,
                "task_queue_move",
                base(
                    task_id=second.id,
                    expected_revision=second.updated.isoformat(),
                    placement=candidate,
                ),
            ), candidate

    def test_two_placements_at_once_do_not_validate(self, service):
        """`before` plus `top` reads fine as separate fields and means nothing."""
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)

        assert schema_rejects(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=second.updated.isoformat(),
                placement={"where": "before", "neighbour": first.id, "top": True},
            ),
        )

    def test_a_neighbour_placement_without_a_neighbour_is_refused(self, service):
        registry, manager, _ = service
        _, second = self._two_ready_tasks(manager)

        assert schema_rejects(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=second.updated.isoformat(),
                placement={"where": "after"},
            ),
        )

    def test_a_missing_placement_is_refused_by_the_handler_too(self, service):
        """Belt and braces: the schema requires it, and the handler does not assume so."""
        registry, manager, _ = service
        _, second = self._two_ready_tasks(manager)

        error = refuse(
            registry,
            "task_queue_move",
            base(task_id=second.id, expected_revision=second.updated.isoformat()),
        )

        assert error.code is ErrorCode.INVALID_INPUT
        assert error.field_errors[0].path == "placement"

    def test_repeating_the_operation_id_replays_instead_of_moving_twice(self, service):
        """A retry after a timeout must not walk the task two places up the band."""
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)
        arguments = base(
            task_id=second.id,
            expected_revision=second.updated.isoformat(),
            placement={"where": "before", "neighbour": first.id},
        )

        applied = call(registry, "task_queue_move", arguments)
        replayed = call(registry, "task_queue_move", dict(arguments))

        assert applied["replayed"] is False
        assert replayed["replayed"] is True
        assert replayed["task"]["queue_position"] == applied["task"]["queue_position"]

    def test_a_stale_revision_is_refused(self, service):
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)
        stale = second.updated.isoformat()
        manager.add_log_entry(second.id, actor="bot", type="note", body="Something happened.")

        error = refuse(
            registry,
            "task_queue_move",
            base(
                task_id=second.id,
                expected_revision=stale,
                placement={"where": "before", "neighbour": first.id},
            ),
        )

        assert error.code is ErrorCode.REVISION_CONFLICT

    def test_it_reports_the_new_place_rather_than_the_lifecycle(self, service):
        """A move changes no state, so "is now ready" would answer a question nobody asked."""
        registry, manager, _ = service
        first, second = self._two_ready_tasks(manager)

        async def run():
            result = await registry.get("task_queue_move").handler(
                base(
                    task_id=second.id,
                    expected_revision=second.updated.isoformat(),
                    placement={"where": "top"},
                )
            )
            return result[0][0].text

        text = anyio.run(run)
        assert text.startswith(f"Moved {second.id} to ")
        assert "is now" not in text


# ---------------------------------------------------------------------------
# The record-quality check on the write path (task-306)
# ---------------------------------------------------------------------------
class TestRecordWarnings:
    """What ``record_check`` says reaches the agent, and only where it was earned.

    The design decision on task-306 is that these fire at the moment a write could have
    caused them and nowhere else, so the tests that matter most here are the silent
    ones: a claim of somebody else's long summary, and every verb that carries no
    ``record_warnings`` key at all.
    """

    def long_summary(self) -> str:
        """A summary one word past the ceiling, built from the constant."""
        return " ".join(["word"] * (SUMMARY_WORD_CEILING + 1))

    def test_a_create_with_a_long_summary_warns_in_payload_and_sentence(self, service):
        registry, _, _ = service

        payload = call(
            registry,
            "task_create_ready",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Long",
                "summary": self.long_summary(),
                "description": "The working spec.",
            },
        )

        assert [item["kind"] for item in payload["record_warnings"]] == [LONG_SUMMARY]
        assert f"{SUMMARY_WORD_CEILING + 1} words" in payload["record_warnings"][0]["message"]

    def test_the_warning_is_in_the_text_a_client_without_structured_results_reads(self, service):
        registry, _, _ = service

        async def run():
            definition = registry.get("task_create_ready")
            content, _ = await definition.handler(
                {
                    "project_id": "solo",
                    "actor": "bot",
                    "operation_id": op(),
                    "title": "Long",
                    "summary": self.long_summary(),
                    "description": "The working spec.",
                }
            )
            return content[0].text

        text = anyio.run(run)

        assert "Created" in text
        assert "spec.summary is" in text

    def test_an_ordinary_create_is_silent(self, service):
        registry, _, _ = service

        payload = call(
            registry,
            "task_create_ready",
            {
                "project_id": "solo",
                "actor": "bot",
                "operation_id": op(),
                "title": "Short",
                "summary": "One sentence that orients a reader.",
                "description": "The working spec.",
            },
        )

        assert payload["record_warnings"] == []

    def test_claiming_somebody_elses_long_summary_carries_no_warning_at_all(self, service):
        registry, manager, _ = service
        manager.create_task(
            id="task-001-work",
            title="Long",
            description="D",
            summary=self.long_summary(),
            lifecycle=Lifecycle.READY,
        )

        payload = call(registry, "task_claim", base())

        assert "record_warnings" not in payload

    def test_logging_against_a_worked_task_reports_the_default_prompt(self, service):
        registry, manager, _ = service
        ready_task(manager)
        call(registry, "task_claim", base())

        payload = call(
            registry,
            "task_log_append",
            base(type="progress", body="Started on it."),
        )

        assert [item["kind"] for item in payload["record_warnings"]] == [DEFAULT_BALL_PROMPT]

    def test_a_second_entry_after_a_stated_prompt_is_silent(self, service):
        registry, manager, _ = service
        task = ready_task(manager)
        call(registry, "task_claim", base())
        stated = manager.handoff(
            task.id,
            actor="bot",
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt="Rebase, then re-run the e2e stage.",
        )

        payload = call(
            registry,
            "task_log_append",
            base(type="progress", body="Rebased.", task_id=stated.id),
        )

        assert payload["record_warnings"] == []

    def test_only_the_authoring_tools_carry_the_key_at_all(self, service):
        """Every other tool omits it, rather than sending an always-empty list.

        An always-empty list would read as "checked, nothing found" on verbs that
        cannot produce either condition, which is a claim the tool has not made. The
        key is optional in the shared output schema for exactly that reason.
        """
        registry, manager, _ = service
        ready_task(manager)

        carries = {
            "task_create_ready": call(
                registry,
                "task_create_ready",
                {
                    "project_id": "solo",
                    "actor": "bot",
                    "operation_id": op(),
                    "title": "T",
                    "summary": "Short.",
                    "description": "D",
                },
            ),
            "task_claim": call(registry, "task_claim", base()),
            "task_log_append": call(registry, "task_log_append", base(body="On it.")),
            "task_update_content": call(
                registry,
                "task_update_content",
                base(
                    expected_revision=manager.get_task("task-001-work").updated.isoformat(),
                    patch={"spec": {"summary": "Short.", "description": "D"}},
                ),
            ),
        }

        assert {name for name, payload in carries.items() if "record_warnings" in payload} == {
            "task_create_ready",
            "task_log_append",
            "task_update_content",
        }
        assert "record_warnings" not in registry.get("task_claim").output_schema["required"]


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
        forbidden_names = {"TaskManager", "SqlTaskStore", "Path", "open"}

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
