"""What a dispatched run is refused, proved the way the audit found it: by asking.

Audit 2026-08-21 finding S-1 was not established by reading the routes -- it was
established by making the requests. So is this. Every test below holds a **real minted
run credential** for a **real run directory**, presents it on the header the dispatched
client presents it on, and goes in over HTTP through the whole application.

The pair of classes is the point. ``TestARunIsRefused`` is the list from the spec, one
test per item. ``TestAnOwnerLosesNothing`` runs the *same* requests from the same socket
with the credential omitted, so "a human loses nothing" is asserted rather than assumed
-- and so a mistake that locks the dashboard out fails here rather than on a phone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.credentials import mint_run_credential
from agentjobs.dispatch.runner import RunDirectory, new_run_id
from agentjobs.principals import RUN_CREDENTIAL_HEADER
from agentjobs.projects import HOME_ENV

LOOPBACK = "127.0.0.1"

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
        {"name": "codex", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """One project served out of a temp directory, with a temp AgentJobs home."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


def owner() -> TestClient:
    """The person at this machine: loopback, no run credential."""
    return TestClient(app, client=(LOOPBACK, 51000))


def dispatched(sandbox: Path, task_id: str, *, agent: str = "claude") -> tuple[TestClient, str]:
    """A client identified as a live run working ``task_id``, and that run's id.

    The credential is minted by the same function the dispatcher calls, against a run
    directory shaped the way the dispatcher leaves one. Nothing here is a stub: if
    ``verify_run_credential`` stopped agreeing with ``mint_run_credential`` these tests
    would resolve to no principal and fail, which is the coupling worth having.
    """
    home = sandbox / "home"
    run_id = new_run_id()
    directory = RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": task_id,
            "project_id": "_local",
            "mode": "session",
            "agent": agent,
            "status": "running",
        },
    )
    token = mint_run_credential(directory.path, run_id)
    assert token, "the run credential must actually mint, or nothing below is tested"
    client = TestClient(app, client=(LOOPBACK, 51000), headers={RUN_CREDENTIAL_HEADER: token})
    return client, run_id


def a_task(client: TestClient, title: str = "Sample") -> str:
    response = client.post(
        "/api/tasks",
        json={
            "title": title,
            "description": "Something to act on.",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def in_review(client: TestClient) -> str:
    """A task whose ball is with a human, so /approve is a request that could succeed."""
    task_id = a_task(client)
    assert client.post(f"/api/tasks/{task_id}/claim", json={"agent": "claude"}).status_code == 200
    assert (
        client.post(
            f"/api/tasks/{task_id}/handoff",
            json={
                "actor": "claude",
                "ball": "human",
                "ball_reason": "review",
                "ball_prompt": "Review the work.",
            },
        ).status_code
        == 200
    )
    return task_id


def refusal(response: Any) -> Dict[str, Any]:
    """Assert a 403 and hand back its body, so each test can name the code it wanted."""
    assert (
        response.status_code == 403
    ), f"expected a refusal, got {response.status_code}: {response.text}"
    body: Dict[str, Any] = response.json()
    assert body.get("detail"), "a refusal with no sentence is the message this task exists to fix"
    return body


class TestARunIsRefused:
    """ac-3: the spec's "may not" list, one request at a time, all with a real
    credential."""

    def test_it_cannot_approve_a_review(self, sandbox: Path) -> None:
        """The circular check this task replaces: the id it would have typed is
        published by ``GET /api/projects``, and it is now worth nothing."""
        task_id = in_review(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(client.post(f"/api/tasks/{task_id}/approve", json={"user": "Jeff Posey"}))

        assert body["code"] == "capability_denied"

    def test_it_cannot_dispatch_a_task(self, sandbox: Path) -> None:
        task_id = in_review(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(client.post(f"/api/tasks/{task_id}/dispatch", json={"user": "Jeff Posey"}))

        assert body["code"] == "capability_denied"

    def test_it_cannot_dispatch_above_the_machine_ceiling(self, sandbox: Path) -> None:
        """task-461's overage, refused to a run in the words the table uses.

        The route rule refuses this before the body is read -- no run holds
        ``dispatch.start`` -- and that is the answer a client gets, so this asserts the
        outcome rather than which of the two locks turned it away. The second lock is
        asserted on its own terms in ``tests/test_capabilities.py``, where widening the
        first is what would break it.
        """
        task_id = in_review(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(
            client.post(
                f"/api/tasks/{task_id}/dispatch",
                json={"user": "Jeff Posey", "over_ceiling": True},
            )
        )

        assert body["code"] == "capability_denied"

    def test_it_cannot_enable_dispatch(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        assert refusal(client.post("/api/dispatch/enable"))["code"] == "capability_denied"
        assert refusal(client.post("/api/dispatch/disable"))["code"] == "capability_denied"

    def test_it_cannot_arm_or_disarm_the_pull_mode(self, sandbox: Path) -> None:
        """task-462's ac-2, and the strongest refusal on this list by consequence.

        Arming is not one purchase, it is a standing authority for the server to keep
        starting runs until a bound runs out. An agent that could arm the machine could
        buy itself an unbounded supply of successors out of one act -- which is the
        unbounded loop design section 2 exists to prevent, reached by a different door.
        """
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        assert (
            refusal(client.post("/api/dispatch/arm", json={"bound_kind": "starts", "starts": 5}))[
                "code"
            ]
            == "capability_denied"
        )
        assert refusal(client.post("/api/dispatch/disarm"))["code"] == "capability_denied"

    def test_it_cannot_switch_on_the_idle_session_sweep(self, sandbox: Path) -> None:
        """Enforcement stops the owner's own sessions, so only the owner turns it on
        (task-447)."""
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        response = client.put("/api/sessions/idle/settings", json={"enforce": True})

        assert refusal(response)["code"] == "capability_denied"

    def test_it_cannot_register_or_initialise_a_project(self, sandbox: Path) -> None:
        """Registering a directory is how the audit's S-6 turned any path on the machine
        into served content."""
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)
        payload = {"path": str(sandbox)}

        assert refusal(client.post("/api/projects", json=payload))["code"] == "capability_denied"
        assert (
            refusal(client.post("/api/projects/init", json=payload))["code"] == "capability_denied"
        )

    def test_it_cannot_repair_the_queue(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(
            client.post(
                "/api/queue/repair",
                json={"actor": "claude", "operation_id": "11111111-1111-1111-1111-111111111111"},
            )
        )

        assert body["code"] == "capability_denied"

    def test_it_cannot_touch_webhooks(self, sandbox: Path) -> None:
        """Whoever can read a webhook back holds the HMAC every receiver trusts."""
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(
            client.post("/api/webhooks", json={"url": "http://example.invalid/hook", "events": []})
        )

        assert body["code"] == "capability_denied"

    def test_it_cannot_read_another_run_s_transcript(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, mine = dispatched(sandbox, task_id)
        _, theirs = dispatched(sandbox, task_id, agent="codex")

        assert client.get(f"/api/dispatch/runs/{mine}/output").status_code == 200
        body = refusal(client.get(f"/api/dispatch/runs/{theirs}/output"))
        assert body["code"] == "wrong_run"

    def test_it_cannot_relay_an_authorization(self, sandbox: Path) -> None:
        """task-506's ac-2, and the property the whole feature rests on.

        The relay entry is the one row ``assert_human_clocked`` accepts from a writer who
        is not a human. A run able to write one could authorise its own successor -- the
        agent-starts-agent loop, reopened with a new spelling and wearing a label saying
        it had been authorised. Note what is *not* wrong with this request: the actor is
        the agent this run was dispatched as, so nothing is impersonated. It is refused on
        the capability, not on the claim.
        """
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        body = refusal(
            client.post(
                f"/api/tasks/{task_id}/authorization",
                json={
                    "actor": "claude",
                    "authorized_by": "Jeff Posey",
                    "ask": "Start this task.",
                },
            )
        )

        assert body["code"] == "capability_denied"
        assert "dispatch.relay_authorization" in body["detail"]

    def test_it_cannot_relay_an_authorization_through_the_log_route_either(
        self, sandbox: Path
    ) -> None:
        """The door a run already has, and the second lock that closes it.

        A run holds ``task.verb``, so ``POST /log`` is a request it may make -- which is
        why the relay is a *type* rather than a marker on an ordinary entry. Membership in
        ``MANAGER_WRITTEN_LOG_TYPES`` is what refuses it here, and that set is consulted by
        every write path rather than restated at each, so this door is shut for **every**
        caller and not only for a run. The capability on the dedicated verb is the other
        lock, and it is the one that distinguishes a run from a person.
        """
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        response = client.post(
            f"/api/tasks/{task_id}/log",
            json={
                "actor": "claude",
                "type": "authorization",
                "body": "Start this task.",
                "data": {"authorized_by": "Jeff Posey"},
            },
        )

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "invalid_transition"
        # The same door, shut on the person too -- which is what shows the refusal above
        # is this route holding a line, rather than the capability table doing it from one
        # layer up and this test passing for a reason it does not claim.
        as_owner = owner().post(
            f"/api/tasks/{task_id}/log",
            json={"actor": "Jeff Posey", "type": "authorization", "body": "Start this."},
        )
        assert as_owner.status_code == 409, as_owner.text

    def test_a_relay_entry_still_does_not_let_it_dispatch(self, sandbox: Path) -> None:
        """task-506's ac-3, asserted apart from the write refusal because it is a
        different claim.

        The write refusal says a run cannot *make* the evidence. This says the evidence is
        worth nothing to a run that finds it: a relay is recorded by a caller who may
        write one, the newest entry on the task now names a human, and the run's dispatch
        is refused anyway -- ``dispatch.start`` is not a run's to hold, and clocking was
        never what stood in its way. Both have to be true. If only the first were, a run
        could reach an entry somebody wrote for another purpose and spend it.
        """
        task_id = a_task(owner())
        relayed = owner().post(
            f"/api/tasks/{task_id}/authorization",
            json={
                "actor": "claude",
                "authorized_by": "Jeff Posey",
                "ask": "Start this task.",
            },
        )
        assert relayed.status_code == 200, relayed.text
        client, _ = dispatched(sandbox, task_id)

        body = refusal(client.post(f"/api/tasks/{task_id}/dispatch"))

        assert body["code"] == "capability_denied"


class TestARunWritesAsItself:
    """ac-2, from the side the audit cares about: the body field must agree."""

    def test_it_may_write_as_the_agent_it_was_dispatched_as(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id, agent="claude")

        response = client.post(
            f"/api/tasks/{task_id}/log",
            json={"actor": "claude", "type": "progress", "body": "Working."},
        )

        assert response.status_code == 200, response.text

    def test_it_may_not_write_as_a_person(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, run_id = dispatched(sandbox, task_id)

        body = refusal(
            client.post(
                f"/api/tasks/{task_id}/log",
                json={"actor": "Jeff Posey", "type": "decision", "body": "I approve."},
            )
        )

        assert body["code"] == "actor_mismatch"
        assert "'Jeff Posey'" in body["detail"]
        assert run_id in body["detail"]

    def test_it_may_not_write_as_a_different_agent(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id, agent="claude")

        body = refusal(
            client.post(
                f"/api/tasks/{task_id}/log",
                json={"actor": "codex", "type": "progress", "body": "Not me."},
            )
        )

        assert body["code"] == "actor_mismatch"
        assert "'codex'" in body["detail"] and "'claude'" in body["detail"]


class TestARunCanStillWork:
    """The other half of a capability model: it has to leave the work possible.

    Without these, "refuse everything" would pass every test above, and the first thing
    anybody would notice is a dispatched agent unable to report what it had done.
    """

    def test_it_can_log_progress_on_its_own_task(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        response = client.post(
            f"/api/tasks/{task_id}/progress",
            json={"author": "claude", "summary": "Did the thing."},
        )

        assert response.status_code == 200, response.text

    def test_it_can_hand_off_and_close_its_own_task(self, sandbox: Path) -> None:
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        assert (
            client.post(f"/api/tasks/{task_id}/claim", json={"agent": "claude"}).status_code == 200
        )
        assert (
            client.post(
                f"/api/tasks/{task_id}/handoff",
                json={
                    "actor": "claude",
                    "ball": "human",
                    "ball_reason": "review",
                    "ball_prompt": "Ready.",
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/tasks/{task_id}/close",
                json={"actor": "claude", "outcome": "completed"},
            ).status_code
            == 200
        )

    def test_it_can_act_on_a_task_that_is_not_its_own(self, sandbox: Path) -> None:
        """Lifted by the owner in task-411. Every verb, not a sample, and none of them
        reaches a merge: approval is the only route that starts a finish, and a run holds
        no review capability (``test_it_cannot_approve_a_review``)."""
        mine = a_task(owner(), "Mine")
        yours = a_task(owner(), "Yours")
        client, _ = dispatched(sandbox, mine)

        attempts = [
            (
                f"/api/tasks/{yours}/queue-move",
                {
                    "actor": "claude",
                    "top": True,
                    "operation_id": "44444444-4444-4444-4444-444444444444",
                },
            ),
            (f"/api/tasks/{yours}/log", {"actor": "claude", "type": "progress", "body": "hi"}),
            (f"/api/tasks/{yours}/claim", {"agent": "claude"}),
            (
                f"/api/tasks/{yours}/handoff",
                {
                    "actor": "claude",
                    "ball": "human",
                    "ball_reason": "review",
                    "ball_prompt": "look",
                },
            ),
            (f"/api/tasks/{yours}/close", {"actor": "claude", "outcome": "superseded"}),
        ]
        for path, payload in attempts:
            response = client.post(path, json=payload)
            assert response.status_code == 200, f"{path}: {response.text}"

        assert owner().get(f"/api/tasks/{yours}").json()["outcome"] == "superseded"

    def test_it_can_edit_another_task(self, sandbox: Path) -> None:
        mine = a_task(owner(), "Mine")
        yours = a_task(owner(), "Yours")
        client, _ = dispatched(sandbox, mine)

        response = client.patch(f"/api/tasks/{yours}", json={"title": "Rewritten"})

        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Rewritten"

    def test_it_can_file_a_new_task(self, sandbox: Path) -> None:
        """Deliberately unscoped: agents file follow-ups, and a new task is nobody's."""
        task_id = a_task(owner())
        client, _ = dispatched(sandbox, task_id)

        assert a_task(client, "Filed by an agent")

    def test_it_can_read_its_own_transcript(self, sandbox: Path) -> None:
        """ac-5: identity-gated rather than denied."""
        task_id = a_task(owner())
        client, run_id = dispatched(sandbox, task_id)

        assert client.get(f"/api/dispatch/runs/{run_id}/output").status_code == 200


class TestAnOwnerLosesNothing:
    """The task's binding constraint, asserted with the same requests that were refused.

    If a capability check would deny something the dashboard does today, the table is
    wrong -- so every refusal above is repeated here without the credential and expected
    to get through the gate. "Through the gate" is the claim: a 409 from a dispatch that
    is not configured on this machine is the route working, and only a 403 would mean
    the owner had lost something.
    """

    def test_an_owner_can_approve(self, sandbox: Path) -> None:
        task_id = in_review(owner())

        response = owner().post(f"/api/tasks/{task_id}/approve", json={"user": "Jeff Posey"})

        assert response.status_code == 200, response.text

    def test_an_owner_can_move_the_queue_and_repair_it(self, sandbox: Path) -> None:
        task_id = a_task(owner())

        assert (
            owner()
            .post(
                f"/api/tasks/{task_id}/queue-move",
                json={
                    "actor": "Jeff Posey",
                    "top": True,
                    "operation_id": "33333333-3333-3333-3333-333333333333",
                },
            )
            .status_code
            == 200
        )
        assert (
            owner()
            .post(
                "/api/queue/repair",
                json={
                    "actor": "Jeff Posey",
                    "operation_id": "22222222-2222-2222-2222-222222222222",
                },
            )
            .status_code
            == 200
        )

    def test_an_owner_can_edit_and_archive_any_task(self, sandbox: Path) -> None:
        task_id = a_task(owner())

        assert owner().patch(f"/api/tasks/{task_id}", json={"title": "Renamed"}).status_code == 200
        assert owner().delete(f"/api/tasks/{task_id}").status_code == 200

    def test_an_owner_reaches_the_dispatch_routes(self, sandbox: Path) -> None:
        """Not 403. What comes back is whatever dispatch configuration says, which on a
        temp home is a refusal from the gate rather than from the capability table."""
        task_id = in_review(owner())

        started = owner().post(f"/api/tasks/{task_id}/dispatch", json={"user": "Jeff Posey"})
        enabled = owner().post("/api/dispatch/enable")

        assert started.status_code != 403, started.text
        assert enabled.status_code != 403, enabled.text

    def test_an_owner_reaches_the_pull_mode_controls(self, sandbox: Path) -> None:
        """Not 403. On a temp home with no dispatch.yaml the gate refuses arming under
        its own code, which is the route working; only a 403 would mean the owner had
        lost the switch the run above was refused."""
        armed = owner().post("/api/dispatch/arm", json={"bound_kind": "starts", "starts": 3})
        disarmed = owner().post("/api/dispatch/disarm")

        assert armed.status_code != 403, armed.text
        assert disarmed.status_code != 403, disarmed.text

    def test_an_owner_can_write_as_an_agent(self, sandbox: Path) -> None:
        """The CLI and MCP run as the person at this machine and attribute to the tool.
        Refusing this would have broken every local agent write and protected nothing."""
        task_id = a_task(owner())

        response = owner().post(
            f"/api/tasks/{task_id}/log",
            json={"actor": "claude", "type": "progress", "body": "From a local tool."},
        )

        assert response.status_code == 200, response.text

    def test_an_owner_can_relay_an_authorization_naming_the_agent_as_its_author(
        self, sandbox: Path
    ) -> None:
        """task-506's ac-1, from the side that has to keep working.

        This is the request an interactive agent session makes: it holds no run
        credential, so it resolves as the person at this machine. The entry it writes is
        signed ``claude`` and names ``Jeff Posey`` inside it -- which is the whole feature.
        Asserting the stored shape rather than the status code, because a 200 over a row
        that recorded the human in ``actor`` would be the bug this replaces.
        """
        task_id = a_task(owner())

        response = owner().post(
            f"/api/tasks/{task_id}/authorization",
            json={
                "actor": "claude",
                "authorized_by": "Jeff Posey",
                "ask": "File this and start it.",
                "surface": "an interactive chat session",
            },
        )

        assert response.status_code == 200, response.text
        entry = owner().get(f"/api/tasks/{task_id}").json()["log"][-1]
        assert entry["type"] == "authorization"
        assert entry["actor"] == "claude"
        assert entry["data"]["authorized_by"] == "Jeff Posey"
        assert entry["body"] == "File this and start it."

    def test_an_owner_cannot_relay_an_authorization_by_an_agent(self, sandbox: Path) -> None:
        """The relay records who authorised; it does not widen who may.

        Refused before anything is written, so a nonsense authoriser never leaves a row in
        an append-only log -- and under the same code the dispatch endpoint's ``user``
        field is refused under, because it is the same rule about the same claim.
        """
        task_id = a_task(owner())

        response = owner().post(
            f"/api/tasks/{task_id}/authorization",
            json={"actor": "claude", "authorized_by": "codex", "ask": "Start this."},
        )

        assert response.status_code == 403, response.text
        assert response.json()["code"] == "authorizer_not_human"
        entries = owner().get(f"/api/tasks/{task_id}").json()["log"]
        assert [entry for entry in entries if entry["type"] == "authorization"] == []

    def test_an_owner_may_still_not_write_as_another_person(self, sandbox: Path) -> None:
        """The one thing a human cannot do -- and on this project it is caught earlier,
        by the vocabulary check, because there is only one person in it."""
        task_id = a_task(owner())

        response = owner().post(
            f"/api/tasks/{task_id}/log",
            json={"actor": "Somebody Else", "type": "note", "body": "Not me."},
        )

        assert response.status_code == 400, response.text
