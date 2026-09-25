"""The plan gate: approving a plan is not approving a merge (task-001).

The first external-project pilot handed a task to a person for design approval before
anything was built. The panel offered the same Approve as a final review, and the route
wrote the merge clearance -- and, since task-312, a receipt that ``standing_approval``
reads as authority to merge. On a project with the scripted finish switched on, that
click would have started a finish against a branch holding nothing but a plan.

So the gate is carried by the record (``human/plan``), the route writes a different
sentence for it, the receipt records ``gate: plan``, and every reader of receipts sees a
plan approval as no approval at all. Each branch here is asserted against its named
constant, never a copy of its wording, so one cannot drift while its test stays green.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.routes.tasks import (
    APPROVAL_CLEARANCE,
    DESIGN_APPROVAL_CLEARANCE,
    PLAN_APPROVAL,
)
from agentjobs.dispatch.approval import (
    FINAL_GATE,
    PLAN_GATE,
    approval_data,
    approval_in,
    standing_approval,
)
from agentjobs.models_v2 import (
    BALL_REASONS,
    Ball,
    BallReason,
    LogEntry,
    LogEntryType,
    Task,
    task_status,
)

CONFIG: Dict[str, Any] = {
    "project_name": "Test",
    "tasks_directory": "tasks",
    "actors": [{"name": "jeff", "kind": "human"}, {"name": "claude", "kind": "agent"}],
    "default_user": "jeff",
}


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every finish the route would start, with the finish offered on this machine.

    Offered, so a plan approval passing ``finishable=True`` shows up here as a spawn --
    which is the failure this file exists to catch.
    """
    calls: List[Dict[str, Any]] = []

    def fake_spawn(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "spawned"

    monkeypatch.setattr("agentjobs.api.routes.tasks.spawn_finish", fake_spawn)
    monkeypatch.setattr("agentjobs.api.routes.tasks.finish_is_offered", lambda project_id: True)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def handed_to(client: TestClient, reason: str, *, kind: str | None = None) -> str:
    """A claimed task the agent has handed to ``human/<reason>``."""
    body: Dict[str, Any] = {
        "title": "Waiting on a human",
        "description": "Handed off.",
        "category": "test",
        "lifecycle": "ready",
    }
    created = client.post("/api/tasks", json=body)
    assert created.status_code == 201, created.text
    task_id = str(created.json()["id"])
    if kind is not None:
        patched = client.patch(f"/api/tasks/{task_id}", json={"kind": kind})
        assert patched.status_code == 200, patched.text
    client.post(f"/api/tasks/{task_id}/claim", json={"agent": "claude"})
    handed = client.post(
        f"/api/tasks/{task_id}/handoff",
        json={
            "actor": "claude",
            "ball": "human",
            "ball_reason": reason,
            "ball_prompt": f"Please look at this ({reason}).",
        },
    )
    assert handed.status_code == 200, handed.text
    return task_id


def read(client: TestClient, task_id: str) -> Dict[str, Any]:
    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


def last_handoff(task: Dict[str, Any]) -> Dict[str, Any]:
    return dict([entry for entry in task["log"] if entry["type"] == "handoff"][-1])


def as_task(record: Dict[str, Any]) -> Task:
    """The API's record as the model, keeping only the fields the model declares."""
    fields = set(Task.model_fields)
    return Task.model_validate({key: value for key, value in record.items() if key in fields})


class TestPlanIsAReason:
    """ac-7: ``plan`` is a human reason on every surface, and nothing existing changes."""

    def test_the_model_scopes_plan_to_the_human(self) -> None:
        assert BallReason.PLAN in BALL_REASONS[Ball.HUMAN]
        assert BallReason.PLAN not in BALL_REASONS[Ball.AGENT]

    def test_rest_accepts_a_handoff_to_human_plan(self, client: TestClient) -> None:
        task = read(client, handed_to(client, "plan"))
        assert (task["ball"], task["ball_reason"]) == ("human", "plan")

    def test_rest_refuses_plan_on_the_agent_side(self, client: TestClient) -> None:
        task_id = handed_to(client, "review")
        response = client.post(
            f"/api/tasks/{task_id}/handoff",
            json={"actor": "jeff", "ball": "agent", "ball_reason": "plan", "ball_prompt": "x"},
        )
        assert response.status_code == 409, response.text
        assert "ball_reason 'plan' does not belong to ball 'agent'" in response.text

    def test_it_reads_needs_plan_approval_everywhere_a_status_is_shown(
        self, client: TestClient
    ) -> None:
        task = read(client, handed_to(client, "plan"))
        assert task["display_status"] == "Needs plan approval"
        chip = task_status(as_task(task))
        assert (chip.label, chip.category.value) == ("Needs plan approval", "needs_you")


class TestPlanApproval:
    def test_it_sends_the_task_back_to_be_built_and_says_nothing_of_merging(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """ac-1 and ac-6: the prompt and entry say what happens next, with no repair."""
        task_id = handed_to(client, "plan")
        response = client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        assert response.status_code == 200, response.text

        task = read(client, task_id)
        assert (task["ball"], task["ball_reason"]) == ("agent", "work")
        assert task["lifecycle"] == "active"
        assert task["ball_prompt"] == PLAN_APPROVAL
        assert "cleared to merge" not in task["ball_prompt"]
        assert "close this task" not in task["ball_prompt"]
        entry = last_handoff(task)
        assert entry["body"] == "Plan approved by jeff through the web UI."
        assert entry["data"]["approval"]["gate"] == PLAN_GATE

    def test_it_never_starts_a_finish(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """ac-2. Fails if the plan branch passes ``finishable=True``: the finish is offered."""
        task_id = handed_to(client, "plan")
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        assert spawned == []

    def test_a_note_rides_along_and_the_approval_is_still_not_merge_clearance(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        task_id = handed_to(client, "plan")
        client.post(
            f"/api/tasks/{task_id}/approve",
            json={"user": "jeff", "note": "Use the existing table.", "gate": "plan"},
        )
        task = read(client, task_id)
        assert task["ball_prompt"].startswith(PLAN_APPROVAL)
        assert "Note from jeff:" in task["ball_prompt"]
        assert "Use the existing table." in task["ball_prompt"]
        assert "cleared to merge" not in task["ball_prompt"]
        assert last_handoff(task)["body"].startswith("Plan approved by jeff")
        assert last_handoff(task)["data"]["approval"]["note"] == "Use the existing table."
        assert spawned == []

    def test_standing_approval_does_not_count_it(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """ac-2: the receipt reader every finish and poller goes through says no."""
        task_id = handed_to(client, "plan")
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        task = as_task(read(client, task_id))
        assert standing_approval(task, CONFIG, project_id="test") is None

    def test_the_final_review_still_needs_its_own_approval(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """ac-2: build, hand to review, approve -- only that last click starts a finish."""
        task_id = handed_to(client, "plan")
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        client.post(
            f"/api/tasks/{task_id}/handoff",
            json={
                "actor": "claude",
                "ball": "human",
                "ball_reason": "review",
                "ball_prompt": "Built; please review.",
            },
        )
        assert standing_approval(as_task(read(client, task_id)), CONFIG, project_id="test") is None
        assert spawned == []

        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff", "gate": "final"})
        task = read(client, task_id)
        assert task["ball_prompt"] == APPROVAL_CLEARANCE
        receipt = standing_approval(as_task(task), CONFIG, project_id="test")
        assert receipt is not None and receipt.approver == "jeff"
        assert len(spawned) == 1


class TestAStalePageIsRefused:
    """ac-5: the declared gate must be the record's, or nothing is written."""

    @pytest.mark.parametrize(
        ("reason", "declared"), [("plan", "final"), ("review", "plan"), ("approval", "plan")]
    )
    def test_a_mismatched_gate_is_a_409_and_writes_nothing(
        self,
        client: TestClient,
        spawned: List[Dict[str, Any]],
        reason: str,
        declared: str,
    ) -> None:
        task_id = handed_to(client, reason)
        before = read(client, task_id)
        response = client.post(
            f"/api/tasks/{task_id}/approve", json={"user": "jeff", "gate": declared}
        )
        assert response.status_code == 409, response.text
        after = read(client, task_id)
        assert after["log"] == before["log"]
        assert (after["ball"], after["ball_reason"]) == ("human", reason)
        assert spawned == []


class TestFinalGate:
    """ac-3 and ac-8: the final gate is today's path, worded for the kind."""

    @pytest.mark.parametrize("reason", ["review", "approval"])
    @pytest.mark.parametrize("declared", [None, "final"])
    def test_an_implementation_task_writes_the_merge_clearance_and_finishes(
        self,
        client: TestClient,
        spawned: List[Dict[str, Any]],
        reason: str,
        declared: str | None,
    ) -> None:
        task_id = handed_to(client, reason)
        payload: Dict[str, Any] = {"user": "jeff"}
        if declared is not None:
            payload["gate"] = declared
        assert client.post(f"/api/tasks/{task_id}/approve", json=payload).status_code == 200
        task = read(client, task_id)
        assert task["ball_prompt"] == APPROVAL_CLEARANCE
        entry = last_handoff(task)
        assert entry["body"] == "Approved by jeff through the web UI."
        assert entry["data"]["approval"]["gate"] == FINAL_GATE
        assert len(spawned) == 1

    def test_a_design_task_writes_its_own_clearance_and_still_finishes(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        task_id = handed_to(client, "review", kind="design")
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff", "gate": "final"})
        task = read(client, task_id)
        assert task["ball_prompt"] == DESIGN_APPROVAL_CLEARANCE
        assert task["ball_prompt"] != APPROVAL_CLEARANCE
        assert last_handoff(task)["body"] == "Approved by jeff through the web UI."
        assert standing_approval(as_task(task), CONFIG, project_id="test") is not None
        assert len(spawned) == 1

    def test_a_design_task_at_the_plan_gate_is_a_plan_approval(
        self, client: TestClient, spawned: List[Dict[str, Any]]
    ) -> None:
        """The gate decides authority; the kind only decides the words at the final gate."""
        task_id = handed_to(client, "plan", kind="design")
        client.post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})
        assert read(client, task_id)["ball_prompt"] == PLAN_APPROVAL
        assert spawned == []


class TestReceipts:
    """``approval_in`` is where every reader asks, so the refusal lives there."""

    def entry(self, **approval: Any) -> LogEntry:
        return LogEntry(
            id=7,
            ts=datetime(2026, 9, 25, tzinfo=timezone.utc),
            actor="jeff",
            type=LogEntryType.HANDOFF,
            body="Approved by jeff through the web UI.",
            data={"ball": "agent", "ball_reason": "work", **approval},
        )

    def test_a_plan_receipt_is_no_approval_even_with_an_approval_shaped_body(self) -> None:
        data = approval_data(approver="jeff", note="", reviewed=[], gate=PLAN_GATE)
        assert approval_in(self.entry(**data), project_id="p", task_id="t") is None

    def test_a_final_receipt_is_an_approval(self) -> None:
        data = approval_data(approver="jeff", note="", reviewed=[])
        receipt = approval_in(self.entry(**data), project_id="p", task_id="t")
        assert receipt is not None and receipt.approver == "jeff"

    def test_a_receipt_from_before_the_gate_existed_still_stands(self) -> None:
        """No migration: a receipt with no ``gate`` was a final approval."""
        raw = {"approval": {"approver": "jeff", "note": "", "reviewed": []}}
        assert approval_in(self.entry(**raw), project_id="p", task_id="t") is not None


def test_the_mcp_handoff_enum_matches_the_model() -> None:
    """ac-7: MCP's target union lists exactly the model's reasons, per holder.

    ``agent/available`` is the one exception, and it is deliberate: that state is
    ``task_release``, not a handoff. Pinned here because nothing tied the two lists
    together, so a new reason could reach every surface but MCP with the suite green.
    """
    from agentjobs.mcp.mutation_tools import HANDOFF_TARGET_SCHEMA

    offered = {
        branch["properties"]["ball"]["const"]: set(branch["properties"]["reason"]["enum"])
        for branch in HANDOFF_TARGET_SCHEMA["oneOf"]
    }
    declared = {
        ball.value: {reason.value for reason in reasons} for ball, reasons in BALL_REASONS.items()
    }
    declared["agent"].discard(BallReason.AVAILABLE.value)
    assert offered == declared
