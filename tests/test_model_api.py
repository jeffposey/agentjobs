"""The two drafting routes, asked over HTTP the way the interface asks them.

The proof that matters here is the one the 2026-08-21 audit's method insists on: a run's
refusal is established by *making the request* with a real minted credential, not by
reading the table. The harness is deliberately the same shape as
``tests/test_run_authorization.py``'s, for that reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.credentials import mint_run_credential
from agentjobs.dispatch.runner import RunDirectory, new_run_id
from agentjobs.modelaccess import config as model_config
from agentjobs.principals import RUN_CREDENTIAL_HEADER
from agentjobs.projects import HOME_ENV

LOOPBACK = "127.0.0.1"

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

DRAFT_JSON = {
    "summary": "A summary that orients a reader with no other context.",
    "intent": "Why this task exists.",
    "description": "## What to do\n\nThe working specification.",
    "constraints": "No schema change.",
    "out_of_scope": "Anything else.",
    "acceptance": ["One verifiable statement.", "A second one."],
}


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """One project and one AgentJobs home, both temporary."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv(model_config.ENV_CREDENTIAL, raising=False)
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


def owner() -> TestClient:
    """The person at this machine: loopback, no run credential."""
    return TestClient(app, client=(LOOPBACK, 51000))


def dispatched(sandbox: Path) -> TestClient:
    """A client the server resolves as a live dispatched run."""
    home = sandbox / "home"
    run_id = new_run_id()
    directory = RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": "task-001-sample",
            "project_id": "_local",
            "mode": "session",
            "agent": "claude",
            "status": "running",
        },
    )
    token = mint_run_credential(directory.path, run_id)
    assert token, "the run credential must actually mint, or nothing below is tested"
    return TestClient(app, client=(LOOPBACK, 51000), headers={RUN_CREDENTIAL_HEADER: token})


def configure(sandbox: Path, **overrides: Any) -> None:
    """Give the temp home a model configuration, credential included."""
    payload = {"version": 1, "api_key": "not-a-real-key", **overrides}
    (sandbox / "home" / model_config.CONFIG_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


class _Reply:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def stub_provider(monkeypatch: pytest.MonkeyPatch, text: str) -> list:
    """Substitute the transport under the route, and capture what was sent.

    Patched at :mod:`urllib.request` rather than passed as an argument, because the
    point of these tests is the route -- which takes no opener and must not grow one.
    """
    captured: list = []

    def send(request, timeout=None):  # noqa: ANN001 - mirrors urlopen's signature
        captured.append(request)
        return _Reply({"content": [{"type": "text", "text": text}]})

    monkeypatch.setattr("agentjobs.modelaccess.client.urllib.request.urlopen", send)
    return captured


# ----- status -----------------------------------------------------------------


class TestTheStatusRoute:
    def test_an_unconfigured_machine_says_so_and_stays_a_200(self, sandbox: Path) -> None:
        """Design §6: no route 500s, no degraded mode, nothing else changes."""
        response = owner().get("/api/model")
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert body["reason"] == model_config.UNCONFIGURED
        assert body["detail"]

    def test_a_configured_machine_reports_available(self, sandbox: Path) -> None:
        configure(sandbox, model="a-model-id")
        body = owner().get("/api/model").json()
        assert body["available"] is True
        assert body["model"] == "a-model-id"
        assert body["reason"] is None

    def test_the_status_never_answers_with_the_credential(self, sandbox: Path) -> None:
        """Design §4: the status route answers a boolean, never the key or a prefix."""
        configure(sandbox, api_key="sk-a-very-distinctive-value")
        raw = owner().get("/api/model").text
        assert "sk-a-very-distinctive-value" not in raw
        assert "sk-a-very" not in raw

    def test_it_is_served_project_scoped_as_well(self, sandbox: Path) -> None:
        assert owner().get("/api/projects/_local/model").status_code == 200


# ----- who may draft ----------------------------------------------------------


class TestOnlyAPersonMayDraft:
    def test_a_run_is_refused(self, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole of what stops an agent spending the operator's model budget.

        Established by asking with a real minted credential, not by reading the table.
        """
        configure(sandbox)
        captured = stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        response = dispatched(sandbox).post(
            "/api/projects/_local/model/draft",
            json={"title": "A title", "description": "A description"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "capability_denied"
        assert captured == [], "a refused run must not have reached the provider"

    def test_the_owner_may(self, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        configure(sandbox)
        stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        response = owner().post(
            "/api/projects/_local/model/draft",
            json={"title": "A title", "description": "A description"},
        )
        assert response.status_code == 200
        assert response.json()["drafted"] is True

    def test_a_run_may_still_read_the_status(self, sandbox: Path) -> None:
        """Gating the read would leave a run unable to discover that it may not draft."""
        assert dispatched(sandbox).get("/api/model").status_code == 200


# ----- drafting ---------------------------------------------------------------


class TestTheDraftRoute:
    def test_it_returns_the_spec_fields(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(sandbox, model="a-model-id")
        stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        body = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "Paging is slow", "description": "it drags at 400 tasks"},
            )
            .json()
        )
        assert body["summary"] == DRAFT_JSON["summary"]
        assert body["acceptance"] == DRAFT_JSON["acceptance"]
        assert body["model"] == "a-model-id"

    def test_the_prompt_carries_what_the_person_typed(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(sandbox)
        captured = stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        owner().post(
            "/api/projects/_local/model/draft",
            json={"title": "Paging is slow", "description": "it drags at 400 tasks"},
        )
        sent = json.loads(captured[0].data.decode("utf-8"))
        prompt = sent["messages"][0]["content"]
        assert "Paging is slow" in prompt
        assert "it drags at 400 tasks" in prompt
        assert "_local" in prompt

    def test_it_writes_no_task(self, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ac-3: nothing is saved. The draft goes to the form, never to the store."""
        configure(sandbox)
        stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        client = owner()
        before = client.get("/api/projects/_local/tasks").json()
        client.post(
            "/api/projects/_local/model/draft",
            json={"title": "A title", "description": "A description"},
        )
        after = client.get("/api/projects/_local/tasks").json()
        assert len(after) == len(before)

    def test_state_the_model_does_not_own_never_reaches_the_response(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-5, over the wire: there is nowhere in the response to put one."""
        configure(sandbox)
        reply: Dict[str, Any] = dict(DRAFT_JSON)
        reply.update(
            {
                "lifecycle": "ready",
                "priority": "critical",
                "parent": "task-001-invented",
                "dependencies": [{"task": "task-002-invented"}],
                "actor": "somebody",
            }
        )
        stub_provider(monkeypatch, json.dumps(reply))
        raw = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "A title", "description": "A description"},
            )
            .text
        )
        for forbidden in ("critical", "task-001-invented", "task-002-invented", "somebody"):
            assert forbidden not in raw

    def test_an_unconfigured_machine_refuses_with_a_reason_not_an_error(
        self, sandbox: Path
    ) -> None:
        response = owner().post(
            "/api/projects/_local/model/draft",
            json={"title": "A title", "description": "A description"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["drafted"] is False
        assert body["reason"] == model_config.UNCONFIGURED
        assert body["summary"] == ""

    def test_the_sentinel_refuses_the_route(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(sandbox)
        captured = stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        (sandbox / "home" / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        body = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "A title", "description": "A description"},
            )
            .json()
        )
        assert body["drafted"] is False
        assert body["reason"] == model_config.SENTINEL
        assert captured == [], "a stopped machine must not have caused model work"

    def test_a_provider_error_body_never_reaches_the_caller(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Design §4, end to end: nothing upstream is forwarded verbatim."""
        import urllib.error

        configure(sandbox, api_key="sk-distinctive")

        def send(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(
                "https://example.invalid",
                401,
                '{"error":{"message":"bad key sk-distinctive"}}',
                {},
                None,
            )

        monkeypatch.setattr("agentjobs.modelaccess.client.urllib.request.urlopen", send)
        raw = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "A title", "description": "A description"},
            )
            .text
        )
        assert "sk-distinctive" not in raw
        assert json.loads(raw)["reason"] == model_config.REFUSED

    def test_a_reply_in_the_wrong_shape_is_malformed(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(sandbox)
        stub_provider(monkeypatch, "Sure! Here is your spec, hope it helps.")
        body = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "A title", "description": "A description"},
            )
            .json()
        )
        assert body["drafted"] is False
        assert body["reason"] == model_config.MALFORMED

    def test_every_reason_it_can_answer_with_is_in_the_closed_set(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(sandbox)
        stub_provider(monkeypatch, "not json")
        body = (
            owner()
            .post(
                "/api/projects/_local/model/draft",
                json={"title": "A title", "description": "A description"},
            )
            .json()
        )
        assert body["reason"] in model_config.REASONS


# ----- a drafted task is an ordinary task -------------------------------------


class TestTheResultingTaskIsIndistinguishable:
    def test_filing_a_draft_goes_through_the_ordinary_create_route(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-7: same schema, same managed path, no marker field.

        The draft is filed by posting exactly the body a hand-written one posts, and the
        resulting record is compared field for field against a hand-written twin. A
        provenance marker -- anything that made an AI-drafted task a second class of
        record -- would show up here as a difference.
        """
        configure(sandbox)
        stub_provider(monkeypatch, json.dumps(DRAFT_JSON))
        client = owner()

        draft = client.post(
            "/api/projects/_local/model/draft",
            json={"title": "Paging is slow", "description": "it drags at 400 tasks"},
        ).json()

        body = {
            "title": "Paging is slow",
            "summary": draft["summary"],
            "description": draft["description"],
            "intent": draft["intent"],
            "constraints": draft["constraints"],
            "out_of_scope": draft["out_of_scope"],
            "acceptance": [
                {"id": f"ac-{index + 1}", "text": text, "status": "pending"}
                for index, text in enumerate(draft["acceptance"])
            ],
            "category": "test",
            "lifecycle": "draft",
        }
        drafted = client.post("/api/projects/_local/tasks", json=body)
        assert drafted.status_code == 201, drafted.text

        by_hand = client.post(
            "/api/projects/_local/tasks",
            json={**body, "title": "Paging is slow, written by hand"},
        )
        assert by_hand.status_code == 201, by_hand.text

        left, right = drafted.json(), by_hand.json()
        assert set(left) == set(right)
        ignored = {"id", "title", "created", "updated", "queue_position", "log"}
        for key in set(left) - ignored:
            assert left[key] == right[key], key
        assert [entry["type"] for entry in left["log"]] == [entry["type"] for entry in right["log"]]
