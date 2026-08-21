"""Tests for human action API endpoints (schema v2)."""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.main import app
from agentjobs.api.dependencies import reset_dependency_cache


@pytest.fixture(autouse=True)
def setup_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set up test environment with temporary directories and a configured user."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    agentjobs_dir = tmp_path / ".agentjobs"
    agentjobs_dir.mkdir()
    (agentjobs_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "jeff", "kind": "human", "display_name": "Jeff"},
                    {"name": "test-agent", "kind": "agent"},
                ],
                "default_user": "jeff",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))

    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def sample_task_in_review(client: TestClient) -> str:
    """Create a task sitting in the human inbox: active, ball human/review."""
    response = client.post(
        "/api/tasks",
        json={
            "title": "Sample Task",
            "description": "Test task",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert response.status_code == 201
    task_id = str(response.json()["id"])

    response = client.post(f"/api/tasks/{task_id}/claim", json={"agent": "test-agent"})
    assert response.status_code == 200
    response = client.post(
        f"/api/tasks/{task_id}/handoff",
        json={
            "actor": "test-agent",
            "ball": "human",
            "ball_reason": "review",
            "ball_prompt": "Review the work and approve or request changes.",
        },
    )
    assert response.status_code == 200
    return task_id


def test_approve_task(client: TestClient, sample_task_in_review: str) -> None:
    """Approving hands the ball back to the owning agent (agent/work), not the pool."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 200
    data = response.json()
    task = data["task"]
    assert task["lifecycle"] == "active"
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "work"
    # The owner survives: approval is not a release back into the pool.
    assert task["assignment"]["owner"] == "test-agent"
    assert any("Approved by jeff" in (entry["body"] or "") for entry in task["log"])


def test_approve_does_not_claim_to_have_merged(
    client: TestClient, sample_task_in_review: str
) -> None:
    """The recorded approval must say a merge is still owed, not that one happened."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "jeff"},
    )

    task = response.json()["task"]
    assert "does not run git" in task["ball_prompt"]


def test_request_changes(client: TestClient, sample_task_in_review: str) -> None:
    """Requesting changes moves the ball to agent/revise with the feedback as the ask."""
    feedback = "Please add more error handling"
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/request-changes",
        json={"user": "jeff", "feedback": feedback},
    )
    assert response.status_code == 200
    task = response.json()["task"]

    assert task["ball"] == "agent"
    assert task["ball_reason"] == "revise"
    # The feedback is the payload of the handoff: it rides in the ball_prompt and log.
    assert task["ball_prompt"] == feedback
    latest = task["log"][-1]
    assert latest["type"] == "handoff"
    assert feedback in (latest["body"] or "")
    assert latest["actor"] == "jeff"


def test_reject_task(client: TestClient, sample_task_in_review: str) -> None:
    """Rejecting closes the task as cancelled and archives it, reason on the record."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/reject",
        json={"user": "jeff", "reason": "Out of scope"},
    )
    assert response.status_code == 200
    task = response.json()["task"]
    assert task["lifecycle"] == "closed"
    assert task["outcome"] == "cancelled"
    assert task["archived"] is True
    assert any("Out of scope" in (entry["body"] or "") for entry in task["log"])


def test_an_actor_id_with_spaces_works_end_to_end(
    client: TestClient, sample_task_in_review: str, tmp_path: Path
) -> None:
    """A name-shaped id survives config, the API, the log and a URL query string.

    The owner chose ids that read as names ("Jeff Posey") over terse ones. A space
    passes through four encoders on the way to a log entry, so this walks the whole
    path rather than trusting any one of them.
    """
    (tmp_path / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "test-agent", "kind": "agent"},
                ],
                "default_user": "Jeff Posey",
            }
        ),
        encoding="utf-8",
    )

    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "Jeff Posey"},
    )
    assert response.status_code == 200
    assert response.json()["task"]["log"][-1]["actor"] == "Jeff Posey"

    # Reloaded from disk, so the YAML round trip is included rather than assumed.
    reloaded = client.get(f"/api/tasks/{sample_task_in_review}").json()
    assert reloaded["log"][-1]["actor"] == "Jeff Posey"

    # And as a URL query value, where the space must be encoded rather than truncating
    # the id at the first word.
    page = client.get(f"/p/_local/tasks/{sample_task_in_review}").text
    assert 'const CURRENT_USER = "Jeff Posey"' in page


def test_an_unconfigured_actor_is_refused(client: TestClient, sample_task_in_review: str) -> None:
    """A typo'd or unknown id is rejected rather than written into the log.

    "human" is the specific value every review action used to be recorded as, so it
    stands in for the whole class: the log is append-only, and an id nobody can resolve
    is permanent once written.
    """
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "human"},
    )

    assert response.status_code == 400
    assert "not an actor in this project" in response.json()["detail"]


def test_the_review_buttons_act_as_the_configured_user(
    client: TestClient, sample_task_in_review: str
) -> None:
    """The rendered page must post the configured id, not the hardcoded string.

    Asserting on what the page will actually send, not merely that a button exists --
    the previous version of this template rendered three buttons that all posted
    `user: 'human'`.
    """
    page = client.get(f"/p/_local/tasks/{sample_task_in_review}").text

    assert 'const CURRENT_USER = "jeff"' in page
    # The exact payload the three buttons used to send. Matching on the whole page for
    # "'human'" is too broad -- the Human View tab legitimately contains it.
    assert "user: 'human'" not in page
    assert "Acting as" in page


def test_detail_contract_includes_identity_parent_and_children(
    client: TestClient, sample_task_in_review: str
) -> None:
    """React receives relationships and the safe acting identity in one snapshot."""
    prerequisite = client.post(
        "/api/tasks",
        json={
            "id": "task-prerequisite",
            "title": "Prerequisite",
            "description": "Must finish first",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert prerequisite.status_code == 201
    updated = client.patch(
        f"/api/tasks/{sample_task_in_review}",
        json={
            "dependencies": [
                {"task": "task-prerequisite", "type": "needs", "note": "Required first."},
                {"task": "task-missing", "type": "blocks", "note": "Bad reference."},
            ]
        },
    )
    assert updated.status_code == 200
    child = client.post(
        "/api/tasks",
        json={
            "title": "Child task",
            "description": "Child body",
            "category": "test",
            "lifecycle": "ready",
            "parent": sample_task_in_review,
            "dependencies": [{"task": sample_task_in_review, "type": "needs"}],
        },
    )
    assert child.status_code == 201

    response = client.get(f"/api/tasks/{sample_task_in_review}/detail")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task"]["id"] == sample_task_in_review
    assert payload["parent_task"] is None
    assert [task["id"] for task in payload["children"]] == [child.json()["id"]]
    assert payload["needs"] == [
        {
            "task_id": "task-prerequisite",
            "title": "Prerequisite",
            "exists": True,
            "state": "open",
            "note": "Required first.",
            "reason": "Needs task-prerequisite; it is still open.",
        }
    ]
    assert [relation["task_id"] for relation in payload["blocks"]] == [
        child.json()["id"],
        "task-missing",
    ]
    assert payload["blocks"][1]["state"] == "missing"
    assert payload["child_dependency_edges"] == [
        {
            "source": sample_task_in_review,
            "target": child.json()["id"],
            "note": None,
            "source_exists": True,
            "target_exists": True,
            "source_contained": False,
            "target_contained": True,
        }
    ]
    assert payload["identity"] == {
        "ok": True,
        "user": "jeff",
        "problem": None,
        "detail": "",
    }


def test_review_actions_are_hidden_when_config_names_nobody(
    client: TestClient, sample_task_in_review: str, tmp_path: Path
) -> None:
    """With no human configured the buttons are withheld, not silently anonymous."""
    (tmp_path / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Test", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )

    page = client.get(f"/p/_local/tasks/{sample_task_in_review}").text

    assert "No user configured" in page
    assert "btn-approve" not in page

    detail = client.get(f"/api/tasks/{sample_task_in_review}/detail").json()
    assert detail["identity"]["ok"] is False
    assert detail["identity"]["user"] is None

    refused = client.post(
        f"/api/tasks/{sample_task_in_review}/approve",
        json={"user": "human"},
    )
    assert refused.status_code == 400
    assert "could not say who took it" in refused.json()["detail"]


def test_approve_nonexistent_task(client: TestClient) -> None:
    """Test approving a nonexistent task returns 404."""
    response = client.post(
        "/api/tasks/nonexistent-task/approve",
        json={"user": "jeff"},
    )
    assert response.status_code == 404


def test_request_changes_without_feedback(client: TestClient, sample_task_in_review: str) -> None:
    """Test that request-changes requires feedback."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/request-changes",
        json={"user": "jeff", "feedback": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 400


def test_reject_without_reason(client: TestClient, sample_task_in_review: str) -> None:
    """Test that reject requires a reason."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/reject",
        json={"user": "jeff", "reason": ""},
    )
    # Should fail validation due to min_length=1
    assert response.status_code == 400


# ----- task-231: one route per human act, and each records what its label says ------


def test_answering_is_recorded_as_an_answer_not_a_revision(
    client: TestClient, sample_task_in_review: str
) -> None:
    """The distinction task-081 entry 26 had to repair in prose, made queryable.

    Nothing the agent did is being rejected here; the human is supplying what it asked
    for. Recorded as `revise` -- the only value available before this -- a cold reader
    could not tell the two apart without reconstructing intent from the wording.
    """
    answer = "Option 2, and skip the third question entirely."
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/answer",
        json={"user": "jeff", "feedback": answer},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "answer"
    # Verbatim, exactly as requested changes carries its feedback.
    assert task["ball_prompt"] == answer
    latest = task["log"][-1]
    assert latest["type"] == "handoff"
    assert answer in (latest["body"] or "")
    assert latest["actor"] == "jeff"


def test_redirecting_is_recorded_as_a_re_brief(
    client: TestClient, sample_task_in_review: str
) -> None:
    """task-222 entry 14 had to supersede entry 13 in prose to say this."""
    instructions = "Do the CLI half first; what you have built so far stands."
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/redirect",
        json={"user": "jeff", "feedback": instructions},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "redirect"
    assert task["ball_prompt"] == instructions
    assert "New instructions from jeff" in (task["log"][-1]["body"] or "")


def test_holding_stops_the_task_and_keeps_the_release_condition(
    client: TestClient, sample_task_in_review: str
) -> None:
    """A hold has to be unmissable in the prompt as well as in the reason.

    The reason is what a query reads and what the dispatch paths refuse on; the prefix
    is for the agent that opens the record and skims the prompt. Both, because either
    one alone has a plausible way to be missed.
    """
    condition = "Wait for the autonomous dispatch fixes to land."
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/hold",
        json={"user": "jeff", "feedback": condition},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "hold"
    assert task["ball_prompt"].startswith("ON HOLD")
    assert condition in task["ball_prompt"]
    assert "Put on hold by jeff" in (task["log"][-1]["body"] or "")
    # The list has to say stopped, not "in progress" -- see models_v2.display_status.
    assert task["display_status"] == "On hold (test-agent)"


def test_resume_releases_a_hold_and_puts_the_task_back_to_work(
    client: TestClient, sample_task_in_review: str
) -> None:
    """Without this the panel could impose a hold it could not lift.

    The review panel returns null once the ball leaves the human, so a held task had no
    control at all until Resume existed. A stop with no way out of the browser is worse
    than the illegible record it replaced.
    """
    client.post(
        f"/api/tasks/{sample_task_in_review}/hold",
        json={"user": "jeff", "feedback": "Wait for the fixes."},
    )

    response = client.post(
        f"/api/tasks/{sample_task_in_review}/resume",
        json={"user": "jeff", "note": "They landed this morning."},
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["ball"] == "agent"
    assert task["ball_reason"] == "work"
    assert "Hold released by jeff" in task["ball_prompt"]
    assert "They landed this morning." in task["ball_prompt"]


def test_resume_needs_no_note(client: TestClient, sample_task_in_review: str) -> None:
    """ "Carry on" is a complete instruction; requiring a sentence to say it is what
    pushed four intents onto one reason in the first place."""
    client.post(
        f"/api/tasks/{sample_task_in_review}/hold",
        json={"user": "jeff", "feedback": "Wait for the fixes."},
    )

    response = client.post(f"/api/tasks/{sample_task_in_review}/resume", json={"user": "jeff"})

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["ball_reason"] == "work"
    assert task["ball_prompt"] == "Hold released by jeff -- resume this task."


@pytest.mark.parametrize("route", ["answer", "redirect", "hold"])
def test_a_send_back_without_a_note_is_refused(
    client: TestClient, sample_task_in_review: str, route: str
) -> None:
    """An answer with no answer in it, or a hold with no release condition, is the
    exact record that cost a session its afternoon. The schema refuses it."""
    response = client.post(
        f"/api/tasks/{sample_task_in_review}/{route}",
        json={"user": "jeff", "feedback": ""},
    )

    # 400 rather than 422: this app maps validation refusals itself, the same way
    # /request-changes has always answered an empty feedback field.
    assert response.status_code == 400
