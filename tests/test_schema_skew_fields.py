"""A client older than the service must survive fields the service added (task-445).

task-375 added ``envelope`` and ``delivery`` to dispatch entries and ``selected`` to a
selection's candidates. task-414's supervisor had started its MCP server before that
merged, and its ``task_log_append`` on a task dispatched afterwards came back
``internal_error``:

    log.10.selection.candidates.0.selected  Extra inputs are not permitted
    log.10.delivery                          Extra inputs are not permitted
    log.10.envelope                          Extra inputs are not permitted

The write had landed. The service validated and stored it; only the client's re-parse of
the echoed task failed, so the supervisor retried over HTTP and the instruction is on
task-419 twice. ``test_schema_skew.py`` is the same defect for a widened enum (task-024).

The skew here is the real one, pointed the other way. This process's models are the
*newer* writer, so the store holds a dispatch entry exactly as current code writes it,
and the older reader is built by removing those three fields from the payload models the
client parses with. :class:`OlderClientTransport` restores the current models for the
duration of each service call, so the service stays the newer side throughout.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import anyio
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import types
from pydantic import ValidationError, create_model

from agentjobs import models_v2
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.manager import TaskManager
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.models_v2 import (
    DispatchCandidateData,
    DispatchData,
    DispatchDeliveryData,
    DispatchEnvelopeData,
    DispatchMode,
    MergeMode,
    DispatchSelectionData,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
    StrictModel,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.schema_tolerance import is_tolerant
from agentjobs.sqlstore import SqlTaskStore
from support import task_store

ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
]
TASK_ID = "task-001-work"


def older(model: type[StrictModel], *, drop: Tuple[str, ...] = (), **retyped: Any) -> Any:
    """``model`` as a build that predates ``drop`` would have declared it."""
    fields: Dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name in drop:
            continue
        fields[name] = (retyped.get(name, info.annotation), info)
    return create_model(f"Older{model.__name__}", __base__=StrictModel, **fields)


OlderCandidate = older(DispatchCandidateData, drop=("selected",))
OlderSelection = older(DispatchSelectionData, candidates=List[OlderCandidate])  # type: ignore[valid-type]
OlderDispatchData = older(
    DispatchData,
    drop=("envelope", "delivery"),
    selection=Optional[OlderSelection],
)

#: Fields only the newer writer knows, as the dispatch entry stores them.
NEWER_FIELDS: Dict[str, Dict[str, Any]] = {
    "envelope": {"execution_id": "exe_16a12809e389bb56", "source": "grant"},
    "delivery": {
        "acknowledged_by": "6d934650",
        "channel": "argv",
        "payload_sha256": "fd2b49e4240c7790aaca98227962caba3bed3302fe2bb597970c9757afd68f5a",
        "merge_mode_delivered": True,
    },
}


@contextmanager
def payload_models(dispatch: Any) -> Iterator[None]:
    """Validate dispatch entries against ``dispatch`` for the duration."""
    previous = models_v2.LOG_PAYLOADS[LogEntryType.DISPATCH]
    models_v2.LOG_PAYLOADS[LogEntryType.DISPATCH] = dispatch
    try:
        yield
    finally:
        models_v2.LOG_PAYLOADS[LogEntryType.DISPATCH] = previous


class OlderClientTransport(httpx.BaseTransport):
    """The real service on current models, answering a client on older ones."""

    def __init__(self, inner: TestClient) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with payload_models(DispatchData):
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
        return httpx.Response(response.status_code, content=response.content)


Skewed = Tuple[ToolRegistry, TaskManager, TaskClient, SqlTaskStore]


@pytest.fixture()
def skewed(tmp_path: Path, monkeypatch) -> Iterator[Skewed]:
    """A task dispatched by current code, served to a client that predates task-375."""
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
    store = task_store(root / "tasks")
    manager = TaskManager(store)
    dispatched_task(manager)

    with TestClient(app) as http:
        with TaskClient("http://testserver", transport=OlderClientTransport(http)) as client:
            with payload_models(OlderDispatchData):
                yield build_registry(client), manager, client, store

    reset_dependency_cache()


def dispatched_task(manager: TaskManager) -> Task:
    """A claimed task whose dispatch entry carries every field task-375 added."""
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
        run_id="run_e25a2532",
        agent="bot",
        runner="claude-opus-5",
        mode=DispatchMode.SESSION,
        merge_mode=MergeMode.AUTOMERGE,
        trigger=DispatchTrigger.CHILD,
        caused_by=1,
        argv=["claude", "--bg", "read the record"],
        cwd="C:/projects/agentjobs",
        git_head="6aea404c",
        selection=DispatchSelectionData(
            group="default",
            source="project",
            candidates=[
                DispatchCandidateData(runner="claude-opus-5", eligible=True, selected=True)
            ],
        ),
        envelope=DispatchEnvelopeData(**NEWER_FIELDS["envelope"]),
        delivery=DispatchDeliveryData(**NEWER_FIELDS["delivery"]),
    )


def stored_task(manager: TaskManager) -> Task:
    """The record read back by the newer side, which is the only side storage is."""
    with payload_models(DispatchData):
        task = manager.get_task(TASK_ID)
    assert task is not None
    return task


def stored_dispatch_row(store: SqlTaskStore) -> str:
    """The dispatch entry's payload exactly as the database holds it."""
    rows = (
        store._connection()
        .execute(
            "SELECT data_json FROM log_entry WHERE project_id = ? AND task_id = ? "
            "AND type = 'dispatch'",
            (store.project_id, TASK_ID),
        )
        .fetchall()
    )
    assert len(rows) == 1
    return str(rows[0]["data_json"])


def served_document(client: TaskClient) -> Dict[str, Any]:
    """The task as the service puts it on the wire, before any client parse."""
    response = client._client.get("/api/projects/solo/tasks/" + TASK_ID)
    response.raise_for_status()
    document: Dict[str, Any] = response.json()
    return document


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke an MCP tool, failing the test with the error if it reports one."""

    async def run():
        result = await registry.get(name).handler(arguments)
        if isinstance(result, types.CallToolResult):
            pytest.fail(f"{name} reported an error: {result.structuredContent}")
        _, structured = result
        return structured

    return anyio.run(run)


def log_append(registry: ToolRegistry, body: str) -> Dict[str, Any]:
    return call(
        registry,
        "task_log_append",
        {
            "project_id": "solo",
            "task_id": TASK_ID,
            "actor": "bot",
            "operation_id": str(uuid.uuid4()),
            "type": "instruction",
            "body": body,
        },
    )


def dispatch_data(document: Mapping[str, Any]) -> Dict[str, Any]:
    entries = [entry for entry in document["log"] if entry["type"] == "dispatch"]
    assert len(entries) == 1
    data: Dict[str, Any] = entries[0]["data"]
    return data


class TestThePremise:
    def test_the_older_reader_really_rejects_this_record(self, skewed) -> None:
        """Guard: without tolerance, this client fails the way the supervisor did. If
        the older models stopped being older, every test below would prove nothing."""
        _, _, client, _ = skewed
        document = served_document(client)
        declared = {key: value for key, value in document.items() if key in Task.model_fields}

        with pytest.raises(ValidationError) as caught:
            Task.model_validate(declared)

        rejected = {
            ".".join(str(part) for part in error["loc"][-1:])
            for error in caught.value.errors()
            if error["type"] == "extra_forbidden"
        }
        assert {"delivery", "envelope", "selected"} <= rejected


class TestAnOlderClientWritesToANewerRecord:
    def test_log_append_through_mcp_succeeds(self, skewed) -> None:
        """a1. The call that came back internal_error."""
        registry, manager, _, _ = skewed

        payload = log_append(registry, "From the supervisor: carry on.")

        assert payload["task"]["log"][-1]["body"] == "From the supervisor: carry on."
        assert stored_task(manager).log[-1].body == "From the supervisor: carry on."

    def test_the_write_is_recorded_once(self, skewed) -> None:
        """The damage the error did: a landed write reported as failed invites a retry."""
        registry, manager, _, _ = skewed
        before = len(stored_task(manager).log)

        log_append(registry, "Once.")

        assert len(stored_task(manager).log) == before + 1

    def test_the_newer_fields_are_byte_for_byte_unchanged_in_the_store(self, skewed) -> None:
        """a2. The older writer must never drop or rewrite what the newer one wrote."""
        registry, _, _, store = skewed
        before = stored_dispatch_row(store)
        for name in NEWER_FIELDS:
            assert f'"{name}"' in before
        assert '"selected": true' in before

        log_append(registry, "A write by older code.")

        assert stored_dispatch_row(store) == before

    def test_the_mcp_answer_still_carries_the_newer_fields(self, skewed) -> None:
        """A log entry's ``data`` is kept raw, so even this reader's echo of the task
        shows what it cannot interpret rather than a record with pieces missing."""
        registry, _, _, _ = skewed

        payload = log_append(registry, "Echo.")

        data = dispatch_data(payload["task"])
        for name, value in NEWER_FIELDS.items():
            assert data[name] == value
        assert data["selection"]["candidates"][0]["selected"] is True

    def test_a_typed_read_survives_too(self, skewed) -> None:
        _, _, client, _ = skewed

        task = client.for_project("solo").get_task(TASK_ID)

        assert task.log[-1].data["delivery"] == NEWER_FIELDS["delivery"]

    def test_the_skew_is_logged_by_field_and_model(self, skewed, caplog) -> None:
        _, _, client, _ = skewed

        with caplog.at_level("WARNING", logger="agentjobs.client"):
            client.for_project("solo").get_task(TASK_ID)

        messages = [record.getMessage() for record in caplog.records]
        assert any("'delivery'" in m and "OlderDispatchData" in m for m in messages)
        assert any("'selected'" in m and "OlderDispatchCandidateData" in m for m in messages)
        assert all(TASK_ID in m for m in messages)


def sample_task_payload(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "schema": 2,
        "id": TASK_ID,
        "title": "Work",
        "created": "2026-09-13T00:00:00+00:00",
        "updated": "2026-09-13T00:00:00+00:00",
        "lifecycle": "active",
        "ball": "agent",
        "ball_reason": "work",
        "ball_prompt": "Do the thing.",
        "priority": "medium",
        "category": "general",
        "assignment": {"owner": "bot", "eligible": []},
        "spec": {"summary": "Work.", "description": "Do the thing."},
        "queue_position": 100,
        "branches": [{"name": "fix/task-001", "status": "active"}],
        "log": [],
    }
    payload.update(overrides)
    return payload


def client_serving(payload: Dict[str, Any]) -> TaskClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return TaskClient(base_url="http://testserver", transport=httpx.MockTransport(handler))


class TestAFieldAddedAnywhereInTheRecord:
    def test_a_nested_model_outside_the_log_tolerates_one(self) -> None:
        """The defect was never specific to dispatch payloads: any nested model a later
        build widens would do it, and the top level was already filtered."""
        payload = sample_task_payload(
            spec={"summary": "Work.", "description": "Do the thing.", "hypothesis": "x"},
            branches=[{"name": "fix/task-001", "status": "active", "worktree": "../w"}],
        )

        with client_serving(payload) as client:
            task = client.get_task(TASK_ID)

        assert task.spec.summary == "Work."
        assert task.branches[0].name == "fix/task-001"


class TestStrictnessThatMustSurvive:
    def test_the_model_still_rejects_an_undeclared_field_outside_the_client(self) -> None:
        """Tolerance is opt-in and scoped. Storage, the manager and the API all validate
        without it, so a misspelt or retired key is still refused by name."""
        payload = sample_task_payload(
            spec={"summary": "Work.", "description": "Do the thing.", "pirority": "high"}
        )

        with pytest.raises(ValidationError) as caught:
            Task.model_validate(payload)

        assert "pirority" in str(caught.value)

    def test_a_dispatch_payload_is_still_strict_on_the_write_path(self) -> None:
        with pytest.raises(ValidationError):
            DispatchData.model_validate(
                {
                    "run_id": "run_1",
                    "agent": "bot",
                    "runner": "claude",
                    "mode": "session",
                    "merge_mode": "review",
                    "trigger": "manual",
                    "caused_by": 1,
                    "argv": ["claude"],
                    "cwd": ".",
                    "git_head": "abc",
                    "envelop": {"source": "grant"},
                }
            )

    def test_tolerance_does_not_outlive_the_parse(self) -> None:
        with client_serving(sample_task_payload()) as client:
            client.get_task(TASK_ID)

        assert not is_tolerant()

    def test_a_malformed_known_field_still_raises_in_the_client(self) -> None:
        """Undeclared keys are dropped; a declared one with a wrong type is not excused."""
        payload = sample_task_payload(branches=[{"name": "fix/task-001", "status": 7}])

        with client_serving(payload) as client:
            with pytest.raises(ValueError):
                client.get_task(TASK_ID)
