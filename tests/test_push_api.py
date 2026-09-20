"""The four push endpoints, and who may call them (task-423).

The interesting cases are the boundaries rather than the happy path: a device is a live
capability to write on somebody's lock screen, so registering one is a human act and
the endpoint that identifies a device is a secret the API must never hand back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from agentjobs.api.authorization import ROUTE_CAPABILITIES
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.capabilities import Capability, GRANTS
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    Priority,
    Spec,
    Task,
)
from agentjobs.principals import PrincipalKind
from agentjobs.projects import ProjectRegistry
from agentjobs.push import delivery, load
from agentjobs.push.webpush import b64url_decode, b64url_encode
from support import task_store

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc-SECRET-123"


def waiting_task(task_id: str) -> Task:
    return Task(
        id=task_id,
        assignment=Assignment(owner="claude"),
        title=f"Title of {task_id}",
        created=NOW,
        updated=NOW,
        lifecycle=Lifecycle.ACTIVE,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Review the branch.",
        priority=Priority.MEDIUM,
        queue_position=int(task_id.split("-")[1]) * 100,
        category="general",
        spec=Spec(summary=f"Summary of {task_id}", description="Body."),
    )


def browser_subscription(endpoint: str = ENDPOINT) -> Dict[str, Any]:
    """Exactly the shape `PushSubscription.toJSON()` produces."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = b64url_encode(
        private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": public, "auth": b64url_encode(b"0123456789abcdef")},
        "label": "Pixel",
    }


@pytest.fixture()
def client_for(tmp_path: Path, monkeypatch):
    """A one-project server whose push state lives under a throwaway home."""

    def build(tasks: List[Task]) -> Tuple[TestClient, Path]:
        home = tmp_path / "home"
        monkeypatch.setenv("AGENTJOBS_HOME", str(home))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()

        root = tmp_path / "inbox"
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        storage = task_store(root / "tasks", project_id="inbox")
        for task in tasks:
            storage.save_task(task)
        ProjectRegistry(home=home).add(root, project_id="inbox")
        return TestClient(app), home

    yield build
    reset_dependency_cache()


class TestTheStatusRead:
    def test_it_publishes_a_key_and_mints_one_on_first_use(self, client_for) -> None:
        """No setup step: the first person to open the panel creates the keypair."""
        client, home = client_for([])
        body = client.get("/api/projects/inbox/push").json()

        assert len(b64url_decode(body["vapid_public_key"])) == 65
        assert body["devices"] == []
        assert (home / "push" / "vapid.json").exists()

    def test_the_key_does_not_change_between_reads(self, client_for) -> None:
        client, _home = client_for([])
        first = client.get("/api/projects/inbox/push").json()["vapid_public_key"]
        assert client.get("/api/projects/inbox/push").json()["vapid_public_key"] == first

    def test_it_says_honestly_that_nothing_is_watching(self, client_for) -> None:
        """A TestClient never enters the lifespan, so the loop is not running -- and a
        panel that claimed otherwise would send somebody debugging silence."""
        client, _home = client_for([])
        assert client.get("/api/projects/inbox/push").json()["watching"] is False


class TestSubscribing:
    def test_a_device_is_registered_and_its_endpoint_is_not_returned(self, client_for) -> None:
        client, home = client_for([])
        answer = client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

        assert answer.status_code == 200
        assert "SECRET" not in answer.text
        devices = answer.json()["devices"]
        assert len(devices) == 1
        assert devices[0]["service"] == "fcm.googleapis.com"
        assert devices[0]["label"] == "Pixel"
        # Naming the task is the default since task-421; a device with bystanders
        # turns privacy on and gets "count".
        assert devices[0]["detail"] == "task"
        assert load("inbox", home=home)[0].endpoint == ENDPOINT

    def test_subscribing_twice_is_one_device(self, client_for) -> None:
        client, _home = client_for([])
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())
        answer = client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())
        assert len(answer.json()["devices"]) == 1

    def test_opting_in_mid_episode_does_not_interrupt(self, client_for) -> None:
        """The person is looking at the page that already says one is waiting."""
        client, home = client_for([waiting_task("task-001")])
        episode = client.get("/api/projects/inbox/attention").json()["episode"]
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

        assert load("inbox", home=home)[0].last_episode_id == episode["id"]

    def test_an_endpoint_that_is_not_https_is_refused(self, client_for) -> None:
        client, _home = client_for([])
        payload = browser_subscription(endpoint="http://somewhere.example/push")
        answer = client.post("/api/projects/inbox/push/subscribe", json=payload)
        assert answer.status_code == 400
        assert "https" in answer.json()["detail"]

    def test_turning_privacy_on_changes_the_row_without_re_arming_it(self, client_for) -> None:
        """The phone panel's toggle, as the server sees it (task-421).

        Re-posting the endpoint is how a device changes what its pushes may say. It has
        to replace the row rather than add one, and it has to keep the episode the
        device has already been told about -- a toggle that cost the person a repeat
        notification would be a toggle nobody touches.
        """
        client, home = client_for([waiting_task("task-001")])
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())
        before = load("inbox", home=home)[0]
        assert before.detail == "task"

        quiet = {**browser_subscription(), "detail": "count"}
        answer = client.post("/api/projects/inbox/push/subscribe", json=quiet)

        assert len(answer.json()["devices"]) == 1
        after = load("inbox", home=home)[0]
        assert after.detail == "count"
        assert after.id == before.id
        assert after.last_episode_id == before.last_episode_id

    def test_an_unknown_detail_mode_is_refused(self, client_for) -> None:
        client, _home = client_for([])
        payload = {**browser_subscription(), "detail": "everything"}
        answer = client.post("/api/projects/inbox/push/subscribe", json=payload)
        assert answer.status_code == 400


class TestUnsubscribing:
    def test_by_id_from_the_page(self, client_for) -> None:
        client, _home = client_for([])
        registered = client.post(
            "/api/projects/inbox/push/subscribe", json=browser_subscription()
        ).json()["devices"][0]

        answer = client.post(
            "/api/projects/inbox/push/unsubscribe",
            json={"subscription_id": registered["id"]},
        )
        assert answer.json()["devices"] == []

    def test_by_endpoint_from_a_service_worker(self, client_for) -> None:
        """`pushsubscriptionchange` knows the endpoint it is losing and nothing else."""
        client, _home = client_for([])
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

        answer = client.post("/api/projects/inbox/push/unsubscribe", json={"endpoint": ENDPOINT})
        assert answer.json()["devices"] == []

    def test_removing_something_already_gone_is_not_an_error(self, client_for) -> None:
        """Both ends routinely fire for one device; a 404 would be an error about a race."""
        client, _home = client_for([])
        answer = client.post(
            "/api/projects/inbox/push/unsubscribe", json={"subscription_id": "dev_nope"}
        )
        assert answer.status_code == 200

    def test_naming_nothing_is_refused(self, client_for) -> None:
        client, _home = client_for([])
        answer = client.post("/api/projects/inbox/push/unsubscribe", json={})
        assert answer.status_code == 400


class TestTheTestPush:
    def test_it_reports_per_device(self, client_for, monkeypatch) -> None:
        client, _home = client_for([])
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

        monkeypatch.setattr(delivery, "send", lambda *_a, **_k: delivery.SendResult("sent", 201))
        answer = client.post("/api/projects/inbox/push/test", json={})
        assert [row["outcome"] for row in answer.json()["results"]] == ["sent"]

    def test_a_failure_is_reported_rather_than_raised(self, client_for, monkeypatch) -> None:
        client, _home = client_for([])
        client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

        monkeypatch.setattr(
            delivery,
            "send",
            lambda *_a, **_k: delivery.SendResult("transient", None, "unreachable"),
        )
        answer = client.post("/api/projects/inbox/push/test", json={})
        assert answer.status_code == 200
        assert answer.json()["results"][0]["error"] == "unreachable"

    def test_no_devices_is_an_empty_answer(self, client_for) -> None:
        client, _home = client_for([])
        assert client.post("/api/projects/inbox/push/test", json={}).json()["results"] == []

    def test_an_unknown_device_is_a_404(self, client_for) -> None:
        client, _home = client_for([])
        answer = client.post("/api/projects/inbox/push/test", json={"subscription_id": "dev_nope"})
        assert answer.status_code == 404


class TestWhoMayCall:
    """A run holds none of this, and the table is what says so.

    Asserted against the capability table rather than by presenting a credential, for
    the reason task-422 gives: the rule is testable without a transport, and the
    transport half is `tests/test_authorization.py`'s subject.
    """

    @pytest.mark.parametrize(
        "route",
        ["get_push_status", "subscribe_push_device", "unsubscribe_push_device", "send_test_push"],
    )
    def test_every_route_needs_push_manage(self, route: str) -> None:
        assert ROUTE_CAPABILITIES[route].capability is Capability.PUSH_MANAGE

    def test_a_run_holds_none_of_it(self) -> None:
        assert Capability.PUSH_MANAGE not in GRANTS[PrincipalKind.RUN]

    def test_both_human_kinds_hold_it(self) -> None:
        assert Capability.PUSH_MANAGE in GRANTS[PrincipalKind.OWNER]
        assert Capability.PUSH_MANAGE in GRANTS[PrincipalKind.TAILNET]


class TestTheDeepLink:
    def test_the_episode_carries_where_a_notification_should_land(self, client_for) -> None:
        """Published by the server because a service worker cannot import the client's
        copy of the rule. The React notifier prefers this over its own."""
        client, _home = client_for([waiting_task("task-001")])
        episode = client.get("/api/projects/inbox/attention").json()["episode"]

        assert episode["deep_link"] == (
            f"/app/p/inbox/tasks/task-001?attention_ack={episode['id']}"
        )

    def test_several_waiting_tasks_link_to_the_filtered_list(self, client_for) -> None:
        """`status=attention` rather than `status=human` since task-499.

        The waiting set now holds claimed tasks nobody is working, whose ball reads
        `agent`, so the older filter would land a person on a list shorter than the
        number the notification had just told them.
        """
        client, _home = client_for([waiting_task("task-001"), waiting_task("task-002")])
        episode = client.get("/api/projects/inbox/attention").json()["episode"]

        assert episode["deep_link"] == (
            f"/app/p/inbox/tasks?status=attention&attention_ack={episode['id']}"
        )


def test_nothing_here_blocks_a_handoff(client_for, monkeypatch) -> None:
    """Delivery is out of band, so a push service having a bad hour cannot reach a task
    write. Asserted by making every send raise and handing a task off anyway."""
    client, _home = client_for([])

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise httpx.ConnectError("the push service is on fire")

    monkeypatch.setattr(delivery, "send", explode)
    client.post("/api/projects/inbox/push/subscribe", json=browser_subscription())

    answer = client.get("/api/projects/inbox/attention")
    assert answer.status_code == 200
