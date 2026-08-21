"""Project routing and actor attribution, from TaskClient through the MCP helpers.

Driven against the real FastAPI application through the real dependency wiring. The
thing most likely to be wrong here is the wiring -- which project a call lands in, and
which config an actor is validated against -- so overriding the manager would test
around the bug rather than at it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.routes.projects import ProjectResponse
from agentjobs.client import ProjectSummary, TaskClient, TaskClientError
from agentjobs.mcp import routing
from agentjobs.mcp.errors import ErrorCode, ToolError
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

SHARED_TASK_ID = "task-001-shared-id"
"""Both projects get a task with this id. Task ids are unique only within a project,
so any surface keying on the id alone fails these tests."""

ALPHA_ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "alpha-bot", "kind": "agent", "display_name": "Alpha Bot"},
]
BETA_ACTORS = [
    {"name": "Grace", "kind": "human", "display_name": "Grace Hopper"},
    {"name": "beta-bot", "kind": "agent", "display_name": "Beta Bot"},
]


def build_project(root: Path, name: str, actors: list, default_user: str) -> None:
    """Create a project directory with an actor vocabulary and one task."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": name,
                "tasks_directory": "tasks",
                "actors": actors,
                "default_user": default_user,
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    TaskStorage(root / "tasks").save_task(
        Task(
            id=SHARED_TASK_ID,
            title=f"{name} task",
            created=now,
            updated=now,
            lifecycle=Lifecycle.READY,
            ball=Ball.AGENT,
            ball_reason=BallReason.AVAILABLE,
            priority=Priority.HIGH,
            queue_position=100,
            category="infrastructure",
            spec=Spec(summary=f"Belongs to {name}", description=f"Belongs to {name}"),
        )
    )


@pytest.fixture()
def two_projects(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TaskClient, TestClient]]:
    """Two registered projects with colliding task ids and different actor lists."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    # Sit outside both projects so nothing resolves a default positionally.
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    build_project(tmp_path / "alpha", "Alpha", ALPHA_ACTORS, "Ada")
    build_project(tmp_path / "beta", "Beta", BETA_ACTORS, "Grace")

    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / "alpha", project_id="alpha")
    registry.add(tmp_path / "beta", project_id="beta")

    with TestClient(app) as http:
        # TestClient is an httpx.Client, so TaskClient can drive the real app in
        # process without binding a socket.
        yield TaskClient("http://testserver", client=http), http

    reset_dependency_cache()


@pytest.fixture()
def local_project(tmp_path: Path, monkeypatch) -> Iterator[TaskClient]:
    """A single-project install pinned by the environment, served as `_local`."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path / "solo"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    reset_dependency_cache()

    build_project(tmp_path / "solo", "Solo", [], "")

    with TestClient(app) as http:
        yield TaskClient("http://testserver", client=http)

    reset_dependency_cache()


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
class TestDiscovery:
    def test_the_client_and_rest_project_models_carry_the_same_fields(self):
        """The duplication between client.py and the API is checked, not asserted.

        ProjectSummary deliberately does not import the API model, so nothing but
        this test stops the two from drifting apart.
        """
        assert set(ProjectSummary.model_fields) == set(ProjectResponse.model_fields)

    def test_discovery_returns_each_projects_own_actor_vocabulary(self, two_projects):
        client, _ = two_projects

        projects = {project.id: project for project in client.projects()}

        assert projects["alpha"].actor_ids == ["Ada", "alpha-bot"]
        assert projects["beta"].actor_ids == ["Grace", "beta-bot"]

    def test_discovery_reports_actor_kinds_and_display_names(self, two_projects):
        client, _ = two_projects

        actors = {actor.id: actor for actor in client.get_project("alpha").actors}

        assert actors["Ada"].kind == "human"
        assert actors["Ada"].display_name == "Ada Lovelace"
        assert actors["alpha-bot"].kind == "agent"
        assert actors["alpha-bot"].display_name == "Alpha Bot"

    def test_discovery_reports_the_human_default_user(self, two_projects):
        client, _ = two_projects

        assert client.get_project("alpha").default_user == "Ada"
        assert client.get_project("beta").default_user == "Grace"

    def test_discovery_never_nominates_an_agent_actor(self, two_projects):
        """default_user exists to address a person, not to be adopted by an agent."""
        client, _ = two_projects

        project = client.get_project("alpha")
        agents = [actor for actor in project.actors if actor.kind == "agent"]

        assert project.default_user not in [actor.id for actor in agents]
        # There is no field that says "use this one".
        assert not any(
            name in ProjectSummary.model_fields
            for name in ("current_actor", "default_agent", "suggested_actor")
        )

    def test_an_unknown_project_names_the_valid_ids(self, two_projects):
        client, _ = two_projects

        with pytest.raises(TaskClientError) as caught:
            client.get_project("gamma")

        message = str(caught.value)
        assert "alpha" in message and "beta" in message


# ----------------------------------------------------------------------------
# Scoped addressing and isolation
# ----------------------------------------------------------------------------
class TestScopedAddressing:
    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda c: c.list_tasks(), id="list_tasks"),
            pytest.param(lambda c: c.get_task(SHARED_TASK_ID), id="get_task"),
            pytest.param(lambda c: c.get_next_task(), id="get_next_task"),
            pytest.param(lambda c: c.search_tasks("task"), id="search_tasks"),
        ],
    )
    def test_every_read_route_parses_the_servers_enriched_response(self, two_projects, call):
        """Read routes answer with the task plus computed fields; Task forbids extras.

        Only display_status was being dropped, so list_tasks raised a five-error
        ValidationError against a real server while the unit tests passed on
        hand-written payloads that carried no computed fields at all. Parametrised
        across the read surface because the next such field will land on all of them.
        """
        client, _ = two_projects

        assert call(client.for_project("alpha")) is not None

    def test_a_scoped_client_addresses_its_own_project(self, two_projects):
        client, _ = two_projects

        alpha = client.for_project("alpha").get_task(SHARED_TASK_ID)
        beta = client.for_project("beta").get_task(SHARED_TASK_ID)

        assert alpha.title == "Alpha task"
        assert beta.title == "Beta task"

    def test_alternating_calls_never_leak_between_projects(self, two_projects):
        """ac-4: the adapter holds no current project, so order cannot matter."""
        client, _ = two_projects
        alpha = client.for_project("alpha")
        beta = client.for_project("beta")

        seen = []
        for _ in range(3):
            seen.append(alpha.get_task(SHARED_TASK_ID).title)
            seen.append(beta.get_task(SHARED_TASK_ID).title)

        assert seen == ["Alpha task", "Beta task"] * 3

    def test_a_write_to_one_project_leaves_the_other_untouched(self, two_projects):
        client, _ = two_projects

        client.for_project("alpha").claim_task(SHARED_TASK_ID, agent="alpha-bot")

        assert client.for_project("alpha").get_task(SHARED_TASK_ID).lifecycle is Lifecycle.ACTIVE
        assert client.for_project("beta").get_task(SHARED_TASK_ID).lifecycle is Lifecycle.READY

    def test_scoping_returns_a_new_client_and_does_not_mutate_the_parent(self, two_projects):
        client, _ = two_projects

        scoped = client.for_project("alpha")

        assert scoped is not client
        assert scoped.project_id == "alpha"
        assert client.project_id is None

    def test_closing_a_scoped_client_leaves_the_parent_usable(self, two_projects):
        client, _ = two_projects

        client.for_project("alpha").close()

        assert client.for_project("beta").get_task(SHARED_TASK_ID).title == "Beta task"

    def test_an_empty_project_id_is_refused_rather_than_defaulted(self, two_projects):
        client, _ = two_projects

        with pytest.raises(TaskClientError, match="never inferred"):
            client.for_project("")

    def test_a_project_id_needing_escaping_is_escaped(self, two_projects):
        client, _ = two_projects

        assert client.for_project("a/b")._path("/tasks") == "/api/projects/a%2Fb/tasks"


# ----------------------------------------------------------------------------
# Actor attribution
# ----------------------------------------------------------------------------
class TestActorAttribution:
    def test_a_configured_actor_is_recorded(self, two_projects):
        client, _ = two_projects

        task = client.for_project("alpha").claim_task(SHARED_TASK_ID, agent="alpha-bot")

        assert task.assignment.owner == "alpha-bot"

    def test_the_other_projects_actor_is_rejected(self, two_projects):
        """Vocabularies are per project; beta-bot is a stranger in alpha."""
        client, _ = two_projects

        with pytest.raises(TaskClientError) as caught:
            client.for_project("alpha").claim_task(SHARED_TASK_ID, agent="beta-bot")

        assert "not an actor in this project" in str(caught.value)

    def test_an_unknown_actor_is_refused_before_the_write(self, two_projects):
        client, _ = two_projects
        alpha = client.for_project("alpha")

        with pytest.raises(TaskClientError):
            alpha.claim_task(SHARED_TASK_ID, agent="nobody")

        # The refusal is not a partial write: the task is exactly as it was.
        task = alpha.get_task(SHARED_TASK_ID)
        assert task.lifecycle is Lifecycle.READY
        assert task.assignment.owner is None

    def test_every_mutating_verb_validates_its_actor(self, two_projects):
        """Claim used to be the only guarded verb; a log entry is forever too."""
        client, _ = two_projects
        alpha = client.for_project("alpha")
        alpha.claim_task(SHARED_TASK_ID, agent="alpha-bot")

        with pytest.raises(TaskClientError):
            alpha.add_log_entry(SHARED_TASK_ID, actor="nobody", body="hello")
        with pytest.raises(TaskClientError):
            alpha.release_task(SHARED_TASK_ID, actor="nobody")
        with pytest.raises(TaskClientError):
            alpha.handoff_task(
                SHARED_TASK_ID,
                actor="nobody",
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="look",
            )
        with pytest.raises(TaskClientError):
            alpha.close_task(SHARED_TASK_ID, actor="nobody")
        with pytest.raises(TaskClientError):
            alpha.add_progress_update(SHARED_TASK_ID, summary="x", agent="nobody")

        assert alpha.get_task(SHARED_TASK_ID).log[-1].type.value == "transition"


# ----------------------------------------------------------------------------
# The shared MCP helpers
# ----------------------------------------------------------------------------
class TestRoutingHelpers:
    def test_a_missing_project_id_is_invalid_input_pointing_at_projects_list(self):
        with pytest.raises(ToolError) as caught:
            routing.require_project_id({})

        error = caught.value
        assert error.code is ErrorCode.INVALID_INPUT
        assert "projects_list" in (error.suggested_action or "")
        assert error.field_errors[0].path == "project_id"

    @pytest.mark.parametrize("value", ["", "   ", None, 7])
    def test_a_blank_or_wrong_typed_project_id_is_refused(self, value):
        with pytest.raises(ToolError):
            routing.require_project_id({"project_id": value})

    def test_an_unknown_project_becomes_a_structured_error_naming_the_choices(self, two_projects):
        client, _ = two_projects

        with pytest.raises(ToolError) as caught:
            routing.resolve_project(client, "gamma")

        error = caught.value
        assert error.code is ErrorCode.UNKNOWN_PROJECT
        assert error.project_id == "gamma"
        assert "alpha" in error.message and "beta" in error.message
        assert error.retryable is False

    def test_a_known_project_resolves_to_its_summary(self, two_projects):
        client, _ = two_projects

        project = routing.resolve_project(client, "beta")

        assert project.id == "beta"
        assert project.actor_ids == ["Grace", "beta-bot"]

    def test_an_unknown_actor_is_rejected_with_the_configured_list(self, two_projects):
        client, _ = two_projects
        project = routing.resolve_project(client, "alpha")

        with pytest.raises(ToolError) as caught:
            routing.require_actor({"actor": "beta-bot"}, project)

        error = caught.value
        assert error.code is ErrorCode.UNKNOWN_ACTOR
        assert "Ada" in error.message and "alpha-bot" in error.message
        assert error.project_id == "alpha"

    def test_guidance_names_agent_actors_without_choosing_the_human(self, two_projects):
        client, _ = two_projects
        project = routing.resolve_project(client, "alpha")

        with pytest.raises(ToolError) as caught:
            routing.require_actor({}, project)

        guidance = caught.value.suggested_action or ""
        assert "alpha-bot" in guidance
        assert "Ada" not in guidance

    def test_a_configured_actor_passes(self, two_projects):
        client, _ = two_projects
        project = routing.resolve_project(client, "alpha")

        assert routing.require_actor({"actor": "alpha-bot"}, project) == "alpha-bot"

    def test_a_project_without_actors_accepts_any_id(self, local_project):
        project = routing.resolve_project(local_project, "_local")

        assert project.actors == []
        assert routing.require_actor({"actor": "whoever"}, project) == "whoever"

    def test_the_payload_validates_against_its_published_schema(self, two_projects):
        import jsonschema

        client, _ = two_projects
        for project in client.projects():
            jsonschema.validate(
                instance=routing.project_payload(project),
                schema=routing.PROJECT_SUMMARY_SCHEMA,
            )


# ----------------------------------------------------------------------------
# Single-project compatibility
# ----------------------------------------------------------------------------
class TestLocalCompatibility:
    def test_the_implicit_project_is_discoverable_as_local(self, local_project):
        projects = local_project.projects()

        assert [project.id for project in projects] == ["_local"]
        assert projects[0].name == "Solo"

    def test_local_is_addressable_by_its_exact_id(self, local_project):
        task = local_project.for_project("_local").get_task(SHARED_TASK_ID)

        assert task.title == "Solo task"

    def test_local_still_answers_unscoped_calls(self, local_project):
        """The unscoped routes stay as they were for existing CLI and GUI callers."""
        assert local_project.get_task(SHARED_TASK_ID).title == "Solo task"

    def test_an_unconfigured_project_still_accepts_writes(self, local_project):
        task = local_project.for_project("_local").claim_task(SHARED_TASK_ID, agent="anyone")

        assert task.assignment.owner == "anyone"
