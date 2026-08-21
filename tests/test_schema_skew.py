"""A client older than the service must still be able to read and write (task-024).

On 2026-08-19, adding ``AUTO = "auto"`` to ``DispatchPosture`` made task-107 unreadable
to every process that had started before the change. The service was fine and serving
the task over ``curl`` the whole time; what failed was ``client.py`` re-validating the
service's already-validated JSON against its own older copy of the schema. The MCP
client's ``task_handoff`` came back ``log.12.posture: Input should be 'read_only',
'supervised' or 'autonomous'`` with ``retryable: false``, so an agent could not record
twenty-five minutes of finished work.

Reproducing that needs two versions of the schema at once, and a test has only one
process. So the skew lives in the transport: :class:`NewerServiceTransport` serves the
real FastAPI application and then rewrites a dispatch entry's posture to a value this
process's ``DispatchPosture`` does not contain. From the client's side that is
indistinguishable from a service that knows a member the client has never heard of --
which is precisely the case that broke, and the only one worth testing, since a fix
that only helps matched versions fixes nothing.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Tuple

import anyio
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import types

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient, TaskClientError
from agentjobs.manager import TaskManager
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import (
    DispatchMode,
    DispatchPosture,
    DispatchTrigger,
    Lifecycle,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
]

#: A posture no member of this process's ``DispatchPosture`` carries. Asserted below
#: rather than assumed, so adding it for real later fails the test that depends on it
#: being unknown instead of quietly making that test prove nothing.
UNKNOWN_POSTURE = "warp_drive"

TASK_ID = "task-001-work"


class NewerServiceTransport(httpx.BaseTransport):
    """The real service, answering as though it knew a posture this process does not.

    Every response body is rewritten on the way back, so the skew is present on reads
    *and* on the task echoed by a mutation -- which is the half that actually broke,
    because the MCP read tools pass the service's JSON through untouched while every
    mutation parses the task out of its envelope.
    """

    def __init__(self, inner: TestClient) -> None:
        """Wrap a live TestClient, whose lifespan the caller owns."""
        self._inner = inner
        self.rewrites = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.request(
            request.method,
            str(request.url),
            content=request.read(),
            headers={
                key: value
                for key, value in request.headers.items()
                if key.lower() not in {"host", "content-length"}
            },
        )
        try:
            payload = response.json()
        except ValueError:
            return httpx.Response(response.status_code, content=response.content)
        return httpx.Response(response.status_code, json=self._rewrite(payload))

    def _rewrite(self, node: Any) -> Any:
        """Recurse into anything, so bare tasks, envelopes and lists are all covered."""
        if isinstance(node, list):
            return [self._rewrite(item) for item in node]
        if not isinstance(node, dict):
            return node
        rewritten = {key: self._rewrite(value) for key, value in node.items()}
        data = rewritten.get("data")
        if rewritten.get("type") == "dispatch" and isinstance(data, dict) and "posture" in data:
            data["posture"] = UNKNOWN_POSTURE
            self.rewrites += 1
        return rewritten


@pytest.fixture()
def skewed(tmp_path: Path, monkeypatch) -> Iterator[Tuple[ToolRegistry, TaskManager, TaskClient]]:
    """The real app behind a transport that answers like a newer service."""
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
        transport = NewerServiceTransport(http)
        with TaskClient("http://testserver", transport=transport) as client:
            yield build_registry(client), manager, client

    reset_dependency_cache()


def dispatched_task(manager: TaskManager) -> Task:
    """A claimed task carrying a dispatch entry, the shape task-107 was in."""
    manager.create_task(
        id=TASK_ID,
        title="Work",
        description="Do the thing.",
        category="general",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(TASK_ID, agent="bot")
    return manager.record_dispatch(
        TASK_ID,
        actor="Ada",
        run_id="run_a1b2c3d4",
        agent="bot",
        runner="claude",
        mode=DispatchMode.SESSION,
        posture=DispatchPosture.SUPERVISED,
        trigger=DispatchTrigger.MANUAL,
        caused_by=1,
        argv=["claude", "--bg", "-p", "read the record"],
        cwd="C:/projects/agentjobs",
        git_head="4887b74",
    )


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke an MCP tool, failing the test if it reports an error."""

    async def run():
        result = await registry.get(name).handler(arguments)
        assert not isinstance(result, types.CallToolResult), "tool reported an error"
        _, structured = result
        return structured

    return anyio.run(run)


def test_the_unknown_posture_really_is_unknown_here() -> None:
    """Guard the premise: every test below is worthless if this value is a member."""
    assert UNKNOWN_POSTURE not in {member.value for member in DispatchPosture}


class TestAnOlderClientReads:
    def test_a_typed_read_survives_a_value_the_client_does_not_know(self, skewed) -> None:
        """ac-1. This is the read that used to fail with the whole task, not one field."""
        _, manager, client = skewed
        dispatched_task(manager)

        task = client.for_project("solo").get_task(TASK_ID)

        assert task.id == TASK_ID
        # The record survives whole: the unknown value is carried verbatim rather than
        # guessed at, and nothing else about the task is lost.
        assert task.log[-1].data["posture"] == UNKNOWN_POSTURE
        assert task.display_status == "In progress (bot)"
        assert task.dispatch_count == 1

    def test_a_listing_still_contains_the_task(self, skewed) -> None:
        """The failure was total -- the task dropped off the board entirely."""
        _, manager, client = skewed
        dispatched_task(manager)

        listed = client.for_project("solo").list_tasks()

        assert [task.id for task in listed] == [TASK_ID]

    def test_the_mcp_read_tool_shows_the_task_as_readable(self, skewed) -> None:
        """The MCP surface an agent actually calls, not just the client method."""
        registry, manager, _ = skewed
        dispatched_task(manager)

        payload = call(registry, "task_get", {"project_id": "solo", "task_id": TASK_ID})

        assert payload["task"]["id"] == TASK_ID
        assert payload["task"]["log"][-1]["data"]["posture"] == UNKNOWN_POSTURE


class TestAnOlderClientStillWrites:
    def test_an_unrelated_mutation_succeeds(self, skewed) -> None:
        """ac-2. The original symptom: a handoff refused, ``retryable: false``, over a
        log field the handoff has no opinion about."""
        registry, manager, _ = skewed
        task = dispatched_task(manager)

        payload = call(
            registry,
            "task_handoff",
            {
                "project_id": "solo",
                "task_id": TASK_ID,
                "actor": "bot",
                "operation_id": str(uuid.uuid4()),
                "expected_revision": task.updated.isoformat(),
                "target": {
                    "ball": "human",
                    "reason": "review",
                    "prompt": "Review the branch; the gate is green.",
                },
            },
        )

        assert payload["task"]["ball"] == "human"
        assert payload["task"]["ball_reason"] == "review"
        # The write reached the file, not just the response.
        assert manager.get_task(TASK_ID).ball_prompt.startswith("Review the branch")

    def test_appending_to_the_log_succeeds(self, skewed) -> None:
        """A second verb, because the defect was in the shared parse, not in one tool."""
        registry, manager, _ = skewed
        dispatched_task(manager)

        call(
            registry,
            "task_log_append",
            {
                "project_id": "solo",
                "task_id": TASK_ID,
                "actor": "bot",
                "operation_id": str(uuid.uuid4()),
                "type": "progress",
                "body": "Twenty-five minutes of work that would otherwise be unrecorded.",
            },
        )

        assert manager.get_task(TASK_ID).log[-1].body.startswith("Twenty-five minutes")

    def test_the_skew_was_actually_present(self, skewed) -> None:
        """Guard the harness: a transport that quietly stopped rewriting would make
        every test above pass for the wrong reason."""
        _, manager, client = skewed
        dispatched_task(manager)

        client.for_project("solo").get_task(TASK_ID)

        assert client._client._transport.rewrites > 0


class TestStrictnessThatMustSurvive:
    def test_writing_an_unknown_enum_value_is_refused(self, skewed) -> None:
        """ac-3. Tolerance is about what a reader accepts, never about what may be
        stored."""
        _, manager, client = skewed
        dispatched_task(manager)

        with pytest.raises(TaskClientError) as caught:
            client.for_project("solo").update_task(TASK_ID, priority="paramount")

        assert caught.value.status_code in (400, 422)

    def test_the_model_still_rejects_an_unknown_value_outside_the_client(self) -> None:
        """The tolerance is opt-in and scoped. Nothing else in the process inherits it,
        which is what keeps ``storage`` and the write path strict."""
        with pytest.raises(ValueError):
            DispatchPosture(UNKNOWN_POSTURE)

    def test_storage_still_refuses_a_file_carrying_one(self, tmp_path: Path) -> None:
        """ac-3, on the path a hand-edited file takes. The service is the authority on
        validity precisely because it does this."""
        storage = TaskStorage(tmp_path)
        good = dispatch_only_task_file(tmp_path)
        good["log"][-1]["data"]["posture"] = UNKNOWN_POSTURE
        (tmp_path / f"{TASK_ID}.yaml").write_text(yaml.safe_dump(good), encoding="utf-8")

        with pytest.raises(Exception) as caught:
            storage.load_task(TASK_ID)

        assert "posture" in str(caught.value)

    def test_a_malformed_response_still_raises(self, skewed) -> None:
        """ac-4's client-side half. Tolerance covers unknown members of known enums; a
        payload that is genuinely broken must still fail loudly rather than arrive as a
        half-built task."""
        _, _, client = skewed

        with pytest.raises(ValueError):
            client._parse_task({"id": "task-001", "title": "No spec, no dates"})


def dispatch_only_task_file(tmp_path: Path) -> Dict[str, Any]:
    """A valid task file on disk, as a dict, ready to be corrupted by one field."""
    storage = TaskStorage(tmp_path)
    manager = TaskManager(storage)
    manager.create_task(
        id=TASK_ID,
        title="Work",
        description="Do the thing.",
        category="general",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(TASK_ID, agent="bot")
    manager.record_dispatch(
        TASK_ID,
        actor="Ada",
        run_id="run_a1b2c3d4",
        agent="bot",
        runner="claude",
        mode=DispatchMode.SESSION,
        posture=DispatchPosture.SUPERVISED,
        trigger=DispatchTrigger.MANUAL,
        caused_by=1,
        argv=["claude"],
        cwd="C:/projects/agentjobs",
        git_head="4887b74",
    )
    loaded: Dict[str, Any] = yaml.safe_load(
        (tmp_path / f"{TASK_ID}.yaml").read_text(encoding="utf-8")
    )
    return loaded


def sample_task_payload(**overrides: Any) -> Dict[str, Any]:
    """One task as the service serves it, for the cases a real service cannot produce.

    A running service will never emit a value its own enums reject, so an unknown
    *top-level* enum -- a ball or a priority a later version added -- can only be
    staged at the wire. That is the same skew as the dispatch posture above, one field
    higher up.
    """
    payload: Dict[str, Any] = {
        "schema": 2,
        "id": TASK_ID,
        "title": "Work",
        "created": "2026-08-19T00:00:00+00:00",
        "updated": "2026-08-19T00:00:00+00:00",
        "lifecycle": "active",
        "ball": "agent",
        "ball_reason": "work",
        "ball_prompt": "Do the thing.",
        "priority": "medium",
        "category": "general",
        "assignment": {"owner": "bot", "eligible": []},
        "spec": {"summary": "Work.", "description": "Do the thing."},
        "queue_position": 100,
        "log": [],
    }
    payload.update(overrides)
    return payload


def client_serving(payload: Dict[str, Any]) -> TaskClient:
    """A client whose service answers every request with this document."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return TaskClient(base_url="http://testserver", transport=httpx.MockTransport(handler))


class TestSkewElsewhereInTheRecord:
    def test_an_unknown_priority_is_carried_verbatim(self) -> None:
        """The defect was never specific to DispatchPosture; any widened enum does it."""
        with client_serving(sample_task_payload(priority="paramount")) as client:
            task = client.get_task(TASK_ID)

        assert task.priority.value == "paramount"
        assert task.priority == "paramount"

    def test_an_unknown_ball_does_not_break_the_consistency_rules(self) -> None:
        """The state axes drive validation rules that index by member, so a ball this
        reader has never heard of would otherwise raise a bare KeyError out of a
        model validator rather than reading at all."""
        payload = sample_task_payload(ball="council", ball_reason="review")

        with client_serving(payload) as client:
            task = client.get_task(TASK_ID)

        assert task.ball == "council"
        # Unknown to this reader, so it has no label for it -- and says so by falling
        # back to the lifecycle rather than inventing one.
        assert task.display_status == "Active"

    def test_the_skew_is_logged_rather_than_swallowed(self, caplog) -> None:
        """Degrading quietly is its own failure: a stale client must remain diagnosable
        without every call having to fail to prove it."""
        payload = sample_task_payload(priority="paramount")

        with caplog.at_level("WARNING", logger="agentjobs.client"):
            with client_serving(payload) as client:
                client.get_task(TASK_ID)

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "paramount" in message and "Priority" in message and TASK_ID in message

    def test_a_matched_client_logs_nothing(self, caplog) -> None:
        """The ordinary case stays silent, or the warning is noise nobody reads."""
        with caplog.at_level("WARNING", logger="agentjobs.client"):
            with client_serving(sample_task_payload()) as client:
                client.get_task(TASK_ID)

        assert caplog.records == []
