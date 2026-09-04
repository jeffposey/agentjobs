"""Two people, one dashboard: each request is attributed to whoever made it.

The unit tests in ``test_actors.py`` prove the resolution. This proves the wiring, and
it is the half that has actually been wrong before: task-064's bug was not a bad
resolver but three review buttons that never asked one, so every approval was logged as
the literal string ``human``. A test that stops at the function would not have caught
that, and would not catch its successor -- a route that resolves the identity correctly
and then attributes the write to the project default anyway.

Every request here goes in over HTTP, from a named socket, with the headers the front
door would set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.identities import IDENTITIES_FILENAME
from agentjobs.principals import IDENTITY_HEADER
from agentjobs.projects import HOME_ENV

LOOPBACK = "127.0.0.1"
OFF_MACHINE = "10.1.2.3"

JEFF_LOGIN = "jeff@example.com"
SAM_LOGIN = "sam@example.com"


def config_with(*actors: dict) -> dict:
    return {
        "project_name": "Test",
        "tasks_directory": "tasks",
        "actors": list(actors),
    }


TWO_PEOPLE = config_with(
    {"name": "jeff", "kind": "human", "display_name": "Jeff"},
    {"name": "sam", "kind": "human", "display_name": "Sam"},
    {"name": "test-agent", "kind": "agent"},
)

SAM_RETIRED = config_with(
    {"name": "jeff", "kind": "human", "display_name": "Jeff"},
    {"name": "sam", "kind": "human", "display_name": "Sam", "retired": True},
    {"name": "test-agent", "kind": "agent"},
)

ONE_PERSON = config_with(
    {"name": "jeff", "kind": "human", "display_name": "Jeff"},
    {"name": "test-agent", "kind": "agent"},
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project root whose config and machine identity map the test writes."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


def write_config(project: Path, config: dict) -> None:
    (project / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def write_identities(project: Path, payload: dict) -> None:
    (project / "home" / IDENTITIES_FILENAME).write_text(yaml.safe_dump(payload), encoding="utf-8")


def map_both(project: Path) -> None:
    write_identities(
        project,
        {
            "identities": [
                {"login": JEFF_LOGIN, "actor": "jeff"},
                {"login": SAM_LOGIN, "actor": "sam"},
            ]
        },
    )


def as_(login: str, *, host: str = LOOPBACK) -> TestClient:
    """A client arriving by the front door's path, carrying a proven login."""
    return TestClient(app, client=(host, 51000), headers={IDENTITY_HEADER: login})


def bare(host: str = LOOPBACK) -> TestClient:
    """A client with no proven identity: the person at the machine."""
    return TestClient(app, client=(host, 51000))


def task_in_review(client: TestClient) -> str:
    response = client.post(
        "/api/tasks",
        json={
            "title": "Sample Task",
            "description": "Test task",
            "category": "test",
            "lifecycle": "ready",
        },
    )
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    assert client.post(f"/api/tasks/{task_id}/claim", json={"agent": "test-agent"}).status_code == (
        200
    )
    assert (
        client.post(
            f"/api/tasks/{task_id}/handoff",
            json={
                "actor": "test-agent",
                "ball": "human",
                "ball_reason": "review",
                "ball_prompt": "Review the work.",
            },
        ).status_code
        == 200
    )
    return task_id


def identity_of(client: TestClient, task_id: str) -> dict:
    response = client.get(f"/api/tasks/{task_id}/detail")
    assert response.status_code == 200, response.text
    return dict(response.json()["identity"])


class TestTwoPeopleAreEachThemselves:
    """ac-1: attribution is per request, not per project."""

    def test_each_page_load_reports_the_caller_it_came_from(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        assert identity_of(as_(JEFF_LOGIN), task_id)["user"] == "jeff"
        assert identity_of(as_(SAM_LOGIN), task_id)["user"] == "sam"

    def test_each_of_them_can_approve_as_themselves(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)

        first = task_in_review(bare())
        assert (
            as_(JEFF_LOGIN).post(f"/api/tasks/{first}/approve", json={"user": "jeff"}).status_code
            == 200
        )

        second = task_in_review(bare())
        assert (
            as_(SAM_LOGIN).post(f"/api/tasks/{second}/approve", json={"user": "sam"}).status_code
            == 200
        )

    def test_the_log_names_the_person_who_actually_approved(self, project: Path) -> None:
        # The point of the whole module. An approval by Sam must not be recorded as
        # Jeff's, whatever the project config says its default is.
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(SAM_LOGIN).post(f"/api/tasks/{task_id}/approve", json={"user": "sam"})

        assert response.status_code == 200
        actors = [entry["actor"] for entry in response.json()["task"]["log"]]
        assert "sam" in actors
        assert "jeff" not in actors

    def test_one_person_cannot_approve_as_the_other(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(SAM_LOGIN).post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})

        # 403 rather than the 400 this answered before task-332: acting as another
        # person is a refusal of the caller, not a malformed request. The body carries
        # both names -- what was claimed and what the request resolved to -- because a
        # misconfigured mapping is the likeliest cause of seeing this.
        assert response.status_code == 403
        body = response.json()
        assert body["code"] == "actor_mismatch"
        assert "'sam'" in body["detail"]
        assert "'jeff'" in body["detail"]

    def test_a_bare_loopback_request_is_refused_rather_than_guessed(self, project: Path) -> None:
        # Nobody said who is at the keyboard and two people could be. The old MULTIPLE
        # refusal is gone; this is what replaced it, and it is narrower by exactly one
        # case -- the identified one, tested above.
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        identity = identity_of(bare(), task_id)

        assert identity["ok"] is False
        assert identity["problem"] == "ambiguous"

    def test_the_machine_owner_resolves_a_bare_loopback_request(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        write_identities(project, {"owner": "jeff"})
        task_id = task_in_review(bare())

        assert identity_of(bare(), task_id)["user"] == "jeff"


class TestTheHeaderIsOnlyBelievedFromTheFrontDoor:
    """task-329's trust rule, re-asserted where it now decides an attribution."""

    def test_a_login_claimed_from_off_the_machine_is_ignored(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        identity = identity_of(as_(SAM_LOGIN, host=OFF_MACHINE), task_id)

        assert identity["ok"] is False
        assert identity["user"] is None

    def test_and_it_cannot_be_used_to_approve(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(SAM_LOGIN, host=OFF_MACHINE).post(
            f"/api/tasks/{task_id}/approve", json={"user": "sam"}
        )

        # Since task-332 this is refused before the identity is even consulted: the
        # request resolved to no principal at all, and the capability gate answers 403
        # naming the problem code rather than letting the route reason about a login it
        # already decided not to believe.
        assert response.status_code == 403
        assert response.json()["code"] == "no_proven_identity"


class TestAnUnmappedLoginIsRefusedNotDefaulted:
    """ac-3, over HTTP, because the fallback would be invisible in a unit test."""

    def test_the_dashboard_refuses_and_names_the_login_and_the_file(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        identity = identity_of(as_("stranger@example.com"), task_id)

        assert identity["ok"] is False
        assert identity["problem"] == "unmapped"
        assert "stranger@example.com" in identity["detail"]
        assert IDENTITIES_FILENAME in identity["detail"]

    def test_it_never_becomes_the_default_user(self, project: Path) -> None:
        # A single-human project with a stated default is where the fallback would be
        # most tempting and most wrong: an unrecognised person would silently act as
        # Jeff, which is exactly the defect task-064 removed.
        write_config(project, {**ONE_PERSON, "default_user": "jeff"})
        write_identities(project, {"identities": [{"login": JEFF_LOGIN, "actor": "jeff"}]})
        task_id = task_in_review(bare())

        identity = identity_of(as_("stranger@example.com"), task_id)

        assert identity["user"] is None
        assert identity["user"] != "jeff"

    def test_and_the_stranger_cannot_approve(self, project: Path) -> None:
        write_config(project, {**ONE_PERSON, "default_user": "jeff"})
        write_identities(project, {"identities": [{"login": JEFF_LOGIN, "actor": "jeff"}]})
        task_id = task_in_review(bare())

        response = as_("stranger@example.com").post(
            f"/api/tasks/{task_id}/approve", json={"user": "jeff"}
        )

        assert response.status_code == 400
        assert "stranger@example.com" in response.json()["detail"]


class TestRetirement:
    """ac-4, against a log written before the retirement."""

    def test_a_person_who_retires_after_acting_leaves_their_entries_intact(
        self, project: Path
    ) -> None:
        # The order matters and is the test: Sam acts first, retires second, and the
        # entry naming him must still be there and still resolve to a human afterwards.
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())
        approved = as_(SAM_LOGIN).post(f"/api/tasks/{task_id}/approve", json={"user": "sam"})
        assert approved.status_code == 200

        write_config(project, SAM_RETIRED)
        reset_dependency_cache()

        detail = bare().get(f"/api/tasks/{task_id}/detail")
        assert detail.status_code == 200
        actors = [entry["actor"] for entry in detail.json()["task"]["log"]]
        assert "sam" in actors

    def test_a_retired_person_cannot_act_again(self, project: Path) -> None:
        write_config(project, SAM_RETIRED)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(SAM_LOGIN).post(f"/api/tasks/{task_id}/approve", json={"user": "sam"})

        assert response.status_code == 400
        assert "retired" in response.json()["detail"]

    def test_the_person_who_remains_is_unambiguous_without_further_config(
        self, project: Path
    ) -> None:
        write_config(project, SAM_RETIRED)
        map_both(project)
        task_id = task_in_review(bare())

        assert identity_of(bare(), task_id)["user"] == "jeff"


class TestASingleHumanConfigIsUntouched:
    """ac-5: nobody edits YAML because this landed. No identities file exists here."""

    def test_the_dashboard_resolves_with_no_machine_configuration_at_all(
        self, project: Path
    ) -> None:
        write_config(project, {**ONE_PERSON, "default_user": "jeff"})
        task_id = task_in_review(bare())

        assert identity_of(bare(), task_id)["user"] == "jeff"

    def test_and_the_review_buttons_still_work(self, project: Path) -> None:
        write_config(project, {**ONE_PERSON, "default_user": "jeff"})
        task_id = task_in_review(bare())

        response = bare().post(f"/api/tasks/{task_id}/approve", json={"user": "jeff"})

        assert response.status_code == 200

    def test_a_lone_human_with_no_default_user_resolves_too(self, project: Path) -> None:
        write_config(project, ONE_PERSON)
        task_id = task_in_review(bare())

        assert identity_of(bare(), task_id)["user"] == "jeff"


class TestNobodyDispatchesInSomebodyElsesName:
    """Audit S-1's dispatch line, closed (task-332).

    ``POST /tasks/{id}/dispatch`` accepts a ``user``: the human whose authorising entry
    the server writes onto the task before starting a run. It has always been validated
    as a configured human -- and ``GET /api/projects`` publishes exactly that list, so
    knowing a valid name was the whole of the check. What was missing is that the caller
    has to *be* them.
    """

    def test_one_person_cannot_dispatch_in_the_other_s_name(self, project: Path) -> None:
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(SAM_LOGIN).post(f"/api/tasks/{task_id}/dispatch", json={"user": "jeff"})

        assert response.status_code == 403, response.text
        body = response.json()
        assert body["code"] == "actor_mismatch"
        assert "'jeff'" in body["detail"] and "'sam'" in body["detail"]

    def test_dispatching_in_your_own_name_reaches_the_dispatch_gates(self, project: Path) -> None:
        """The contrast that makes the test above mean something.

        Not a 200: this temp home configures no runner, so the machine-level gate
        refuses with ``not_configured``. That is the request getting *through* identity
        and being answered by the layer that should answer it.
        """
        write_config(project, TWO_PEOPLE)
        map_both(project)
        task_id = task_in_review(bare())

        response = as_(JEFF_LOGIN).post(f"/api/tasks/{task_id}/dispatch", json={"user": "jeff"})

        assert response.status_code != 403, response.text
        assert response.json()["code"] == "not_configured"
