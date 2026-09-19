"""Who gets a push, what it says, and what happens when the push service says no.

The policy under test is **task-422's**, unchanged: one interruption per episode, three
deliberate acts acknowledge it, and the waiting set emptying resets it. What task-423
adds is a second kind of client for that one episode, so these cases are about the
things a phone has and a browser tab does not -- a subscription that expires, a service
that is unreachable, a lock screen somebody else can read, and a device registered
halfway through an episode.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agentjobs.attention import (
    acknowledge,
    owes_notification,
    read_episode,
    reconcile,
    waiting_path,
)
from agentjobs.attention import Episode
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.push import (
    DETAIL_TASK,
    Subscription,
    delivery,
    load,
    payload_for,
    remove,
    send_test,
    subscriptions,
    upsert,
)
from agentjobs.push.keys import load_or_create
from agentjobs.push.webpush import b64url_encode, decrypt
from agentjobs.store_factory import server_process
from support import task_store

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
PROJECT = "inbox"


# ----- fixtures ---------------------------------------------------------------------


def waiting_task(task_id: str, *, title: str = "") -> Task:
    return Task(
        id=task_id,
        assignment=Assignment(owner="claude"),
        title=title or f"Title of {task_id}",
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


class FakeManager:
    """Just enough manager for :func:`human_waiting_tasks`.

    A real store would make every case below pay for a database to say something about
    a set of task ids. ``reconcile`` reads two methods: the tasks, and each candidate's
    children -- the second since task-467, because a parent merely waiting on a child a
    person already holds is not a second thing for that person to do.
    """

    def __init__(self, tasks: List[Task]) -> None:
        self._tasks = tasks

    def list_tasks(self, **_kwargs: Any) -> List[Task]:
        return list(self._tasks)

    def get_subtasks(self, task_id: str) -> List[Task]:
        return [task for task in self._tasks if task.parent == task_id]

    def set(self, tasks: List[Task]) -> None:
        self._tasks = tasks


def fake_manager(tasks: List[Task]) -> Any:
    """A :class:`FakeManager` handed over as the real thing.

    Deliberately ``Any``: the stand-in satisfies the one method ``reconcile`` reads and
    nothing else, and casting it at each of a dozen call sites would say the same thing
    twelve times.
    """
    return FakeManager(tasks)


def browser_keys() -> Tuple[ec.EllipticCurvePrivateKey, str, bytes]:
    """A receiving keypair and auth secret, the way a browser produces one."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = b64url_encode(
        private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    return private, public, b"0123456789abcdef"


def device(endpoint: str = "https://fcm.example/send/one", **overrides: Any) -> Subscription:
    _private, public, auth = browser_keys()
    base: Dict[str, Any] = {
        "id": overrides.pop("id", "dev_one"),
        "endpoint": endpoint,
        "p256dh": public,
        "auth": b64url_encode(auth),
        "label": "Pixel",
    }
    base.update(overrides)
    return Subscription(**base)


def transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def always(status: int, text: str = "") -> Callable[[httpx.Request], httpx.Response]:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return handle


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway AgentJobs home, so nothing here touches the real one."""
    target = tmp_path / "home"
    monkeypatch.setenv("AGENTJOBS_HOME", str(target))
    target.mkdir(parents=True, exist_ok=True)
    return target


# ----- what a push says -------------------------------------------------------------


class TestThePayload:
    """A wake-up signal, and on a lock screen a quiet one by default."""

    def _state(self, tasks: List[Task], home: Path):
        return reconcile(fake_manager(tasks), PROJECT, home=home, now=NOW)

    def test_the_default_names_no_task_but_still_says_what_is_wanted(self, home: Path) -> None:
        """The whole of the lock-screen privacy rule, in one assertion.

        The desktop toast names the lead task because it appears on a screen you are
        sitting in front of; a push appears wherever the phone is.

        What the default *does* carry, since task-421, is the ask. "Needs review" is not
        task content -- it says what is wanted, never what the work is -- and it is the
        part that decides whether the person goes and finds a computer. Without it the
        default spent its one line telling them to open the app, which they had already
        worked out from being interrupted.
        """
        state = self._state([waiting_task("task-001", title="Rotate the signing key")], home)
        payload = payload_for(state, PROJECT, detail="count")

        assert payload is not None
        assert payload.title == "1 task is waiting on you"
        assert payload.body == "Needs review"
        assert "Rotate the signing key" not in json.dumps(payload.encode(PROJECT).decode())
        # The id is in the deep link by design and always has been; what the rule covers
        # is the text a bystander can read without unlocking the phone.
        assert "task-001" not in payload.title
        assert "task-001" not in payload.body

    def test_a_task_with_no_ask_falls_back_rather_than_printing_a_placeholder(
        self, home: Path
    ) -> None:
        """A reason with no phrase adds no line.

        Every human-ball reason is mapped today, so what this guards is the next one
        somebody adds without touching ``ASK_PHRASES``: the notification should lose the
        ask and keep working, not print "Needs " or a bare dash.
        """
        task = waiting_task("task-001").model_copy(update={"ball_reason": BallReason.WORK})
        state = self._state([task], home)
        payload = payload_for(state, PROJECT, detail="count")

        assert payload is not None
        assert payload.body == "Open AgentJobs to see what has stopped."

    def test_a_device_may_opt_into_the_detail(self, home: Path) -> None:
        state = self._state(
            [waiting_task("task-001", title="Rotate the signing key"), waiting_task("task-002")],
            home,
        )
        payload = payload_for(state, PROJECT, detail=DETAIL_TASK)

        assert payload is not None
        assert payload.title == "2 tasks are waiting on you"
        assert payload.body == "Needs review — task-001: Rotate the signing key — and 1 other."

    def test_it_deep_links_to_the_one_waiting_task(self, home: Path) -> None:
        state = self._state([waiting_task("task-007")], home)
        payload = payload_for(state, PROJECT, detail="count")

        assert payload is not None
        assert payload.url.startswith("/app/p/inbox/tasks/task-007?attention_ack=att_")
        assert payload.url.endswith(payload.episode_id)

    def test_several_waiting_tasks_go_to_the_filtered_list(self, home: Path) -> None:
        state = self._state([waiting_task("task-001"), waiting_task("task-002")], home)
        payload = payload_for(state, PROJECT, detail="count")

        assert payload is not None
        assert payload.url.startswith("/app/p/inbox/tasks?status=human&attention_ack=att_")

    def test_the_deep_link_matches_what_the_endpoint_publishes(self, home: Path) -> None:
        """One rule, one authority. The service worker reads `deep_link`; this is it."""
        state = self._state([waiting_task("task-007")], home)
        payload = payload_for(state, PROJECT, detail="count")
        assert state.episode is not None and payload is not None
        assert payload.url == waiting_path(PROJECT, state.episode)

    def test_nothing_waiting_is_nothing_to_say(self, home: Path) -> None:
        assert payload_for(self._state([], home), PROJECT, detail="count") is None

    def test_the_tag_is_the_one_the_desktop_uses(self) -> None:
        """So an installed desktop PWA receiving both sees an update, not two entries."""
        assert delivery.notification_tag("inbox") == "agentjobs-attention-inbox"

    def test_the_body_is_encrypted_end_to_end(self, home: Path) -> None:
        """The push service routes it and cannot read it. Asserted by decrypting it."""
        private, public, auth = browser_keys()
        state = self._state([waiting_task("task-001")], home)
        payload = payload_for(state, PROJECT, detail="count")
        assert payload is not None

        captured: List[bytes] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request.content)
            assert request.headers["content-encoding"] == "aes128gcm"
            assert request.headers["authorization"].startswith("vapid t=")
            assert request.headers["ttl"] == str(delivery.DEFAULT_TTL_SECONDS)
            return httpx.Response(201)

        with transport(handle) as client:
            result = delivery.send(
                Subscription(
                    id="dev",
                    endpoint="https://fcm.example/send/x",
                    p256dh=public,
                    auth=b64url_encode(auth),
                ),
                payload.encode(PROJECT),
                key=load_or_create(home=home),
                client=client,
            )

        assert result.outcome == "sent"
        decoded = json.loads(decrypt(captured[0], receiver_private_key=private, auth_secret=auth))
        assert decoded["blocking"] == 1
        assert decoded["episode"] == payload.episode_id


# ----- the devices ------------------------------------------------------------------


class TestTheDeviceStore:
    def test_the_endpoint_is_the_identity(self, home: Path) -> None:
        """Re-subscribing one device is one row, not two, however often it happens."""
        upsert(PROJECT, device(id="dev_a"), home=home)
        upsert(PROJECT, device(id="dev_b"), home=home)

        rows = load(PROJECT, home=home)
        assert len(rows) == 1
        # The original id and episode survive, so a page showing this device does not
        # see it jump and a re-subscribe cannot re-arm a push already delivered.
        assert rows[0].id == "dev_a"

    def test_re_subscribing_keeps_the_episode_already_notified(self, home: Path) -> None:
        upsert(PROJECT, device(last_episode_id="att_seen"), home=home)
        upsert(PROJECT, device(id="dev_new"), home=home)
        assert load(PROJECT, home=home)[0].last_episode_id == "att_seen"

    def test_two_devices_are_two_rows(self, home: Path) -> None:
        upsert(PROJECT, device("https://a.example/1", id="dev_a"), home=home)
        upsert(PROJECT, device("https://b.example/2", id="dev_b"), home=home)
        assert {row.id for row in load(PROJECT, home=home)} == {"dev_a", "dev_b"}

    def test_removal_is_idempotent_from_either_end(self, home: Path) -> None:
        upsert(PROJECT, device(), home=home)
        assert remove(PROJECT, subscription_id="dev_one", home=home) is True
        assert remove(PROJECT, subscription_id="dev_one", home=home) is False
        assert remove(PROJECT, endpoint="https://fcm.example/send/one", home=home) is False

    def test_a_half_written_row_is_dropped_rather_than_served(self, home: Path) -> None:
        """Unusable without the keys, and one click to replace. Raising would take the
        panel down over a row that cannot be repaired from here."""
        path = subscriptions.subscriptions_path(PROJECT, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {"subscriptions": [{"endpoint": "https://a.example/1"}, {"nonsense": True}]}
            ),
            encoding="utf-8",
        )
        assert load(PROJECT, home=home) == ()

    def test_the_endpoint_is_never_in_the_view(self, home: Path) -> None:
        """It is a capability to notify, so it is treated the way a secret is."""
        view = device("https://fcm.example/send/SECRET-TOKEN").view()
        assert "SECRET-TOKEN" not in json.dumps(view, default=str)
        assert view["service"] == "fcm.example"

    def test_a_project_id_that_is_a_path_is_refused(self, home: Path) -> None:
        with pytest.raises(ValueError, match="cannot be used as a filename"):
            subscriptions.subscriptions_path("../escape", home=home)

    def test_the_reserved_local_project_is_allowed(self, home: Path) -> None:
        """`_local` is a filename here and not a slug -- the same guard as attention."""
        assert subscriptions.subscriptions_path("_local", home=home).name == "_local.yaml"


# ----- who is owed one --------------------------------------------------------------


class TestTheRound:
    def _round(self, manager: Any, home: Path, handler, **kwargs):
        with transport(handler) as client:
            return delivery.deliver_round(
                manager, PROJECT, home=home, client=client, key=load_or_create(home=home), **kwargs
            )

    def test_one_push_per_episode(self, home: Path) -> None:
        """The anti-fatigue rule, which is the reason the episode exists.

        A second task stopping during an unacknowledged episode moves the number and
        interrupts nothing -- exactly as on the desktop, because it is the same episode.
        """
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])

        first = self._round(manager, home, always(201))
        assert (first.attempted, first.sent) == (1, 1)

        manager.set([waiting_task("task-001"), waiting_task("task-002")])
        second = self._round(manager, home, always(201))
        assert second.attempted == 0
        assert second.blocking == 2

    def test_acknowledging_re_arms_the_next_episode(self, home: Path) -> None:
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])
        self._round(manager, home, always(201))

        episode = read_episode(PROJECT, home=home)
        assert episode is not None
        acknowledge(manager, PROJECT, episode.id, home=home)

        manager.set([waiting_task("task-001"), waiting_task("task-002")])
        third = self._round(manager, home, always(201))
        assert (third.attempted, third.sent) == (1, 1)

    def test_clearing_everything_resets_and_the_next_wait_alerts(self, home: Path) -> None:
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])
        self._round(manager, home, always(201))

        manager.set([])
        assert self._round(manager, home, always(201)).episode_id is None

        manager.set([waiting_task("task-009")])
        assert self._round(manager, home, always(201)).sent == 1

    def test_an_acknowledged_episode_is_not_pushed_at_all(self, home: Path) -> None:
        """A person who acted on the desktop toast is not then woken on their phone."""
        manager = fake_manager([waiting_task("task-001")])
        reconcile(manager, PROJECT, home=home, now=NOW)
        episode = read_episode(PROJECT, home=home)
        assert episode is not None
        acknowledge(manager, PROJECT, episode.id, home=home)
        upsert(PROJECT, device(), home=home)

        assert self._round(manager, home, always(201)).attempted == 0

    def test_a_restart_replays_nothing(self, home: Path) -> None:
        """The episode and the last-notified id are both on disk, so a fresh process
        picks up where the last one stopped rather than starting the episode again."""
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])
        self._round(manager, home, always(201))

        # A new process: nothing in memory, everything read back off disk.
        assert (
            self._round(fake_manager([waiting_task("task-001")]), home, always(201)).attempted == 0
        )

    def test_a_device_registered_mid_episode_waits_for_the_next_one(self, home: Path) -> None:
        """Seeded at subscribe time, which is what the endpoint does. The person is at
        the page that already says three are waiting; interrupting them is noise."""
        manager = fake_manager([waiting_task("task-001")])
        reconcile(manager, PROJECT, home=home, now=NOW)
        episode = read_episode(PROJECT, home=home)
        assert episode is not None
        upsert(PROJECT, device(last_episode_id=episode.id), home=home)

        assert self._round(manager, home, always(201)).attempted == 0

    def test_two_devices_are_each_owed_their_own(self, home: Path) -> None:
        upsert(PROJECT, device("https://a.example/1", id="dev_a"), home=home)
        upsert(PROJECT, device("https://b.example/2", id="dev_b"), home=home)
        result = self._round(fake_manager([waiting_task("task-001")]), home, always(201))
        assert (result.attempted, result.sent) == (2, 2)

    def test_no_devices_still_advances_the_episode(self, home: Path) -> None:
        """So a device registered a minute later is not told about it as if it were new."""
        with transport(always(201)):
            result = delivery.deliver_round(
                fake_manager([waiting_task("task-001")]), PROJECT, home=home
            )
        assert result.episode_id is not None
        assert result.attempted == 0


# ----- when it goes wrong -----------------------------------------------------------


class TestFailures:
    def _round(self, manager: Any, home: Path, handler):
        with transport(handler) as client:
            return delivery.deliver_round(
                manager, PROJECT, home=home, client=client, key=load_or_create(home=home)
            )

    @pytest.mark.parametrize("status", [404, 410])
    def test_an_expired_endpoint_is_forgotten(self, home: Path, status: int) -> None:
        """The only thing that deletes a device. The push service is the authority on
        whether an endpoint still exists, and nothing else is."""
        upsert(PROJECT, device(), home=home)
        result = self._round(fake_manager([waiting_task("task-001")]), home, always(status))

        assert result.pruned == 1
        assert load(PROJECT, home=home) == ()

    def test_a_refusal_is_recorded_and_not_retried(self, home: Path) -> None:
        """A service that has made up its mind will say the same thing next time, and a
        retry loop against one is how an application server gets its key blocked."""
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])
        first = self._round(manager, home, always(401, "bad VAPID"))

        assert (first.failed, first.sent) == (1, 0)
        row = load(PROJECT, home=home)[0]
        assert row.last_status == 401
        assert "bad VAPID" in (row.last_error or "")
        assert row.last_episode_id is not None

        assert self._round(manager, home, always(201)).attempted == 0

    def test_an_unreachable_service_is_retried_while_the_work_is_still_waiting(
        self, home: Path
    ) -> None:
        """A phone in a tunnel, and the bound on retrying is the episode rather than a
        counter: it stops the moment the person is no longer waited on."""
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])

        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        first = self._round(manager, home, refuse)
        assert first.failed == 1
        row = load(PROJECT, home=home)[0]
        assert row.last_episode_id is None
        assert row.consecutive_failures == 1

        # Due again once the backoff has elapsed, and then delivered.
        with transport(always(201)) as client:
            later = delivery.deliver_round(
                manager,
                PROJECT,
                home=home,
                client=client,
                key=load_or_create(home=home),
                now=datetime.now(tz=timezone.utc) + timedelta(hours=2),
            )
        assert later.sent == 1
        assert load(PROJECT, home=home)[0].consecutive_failures == 0

    def test_a_transient_failure_backs_off_rather_than_hammering(self, home: Path) -> None:
        upsert(
            PROJECT,
            device(consecutive_failures=4, last_attempt_at=datetime.now(tz=timezone.utc)),
            home=home,
        )
        calls: List[int] = []

        def count(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503)

        assert self._round(fake_manager([waiting_task("task-001")]), home, count).attempted == 0
        assert calls == []

    def test_repeated_failure_is_reported_not_removed(self, home: Path) -> None:
        """A laptop closed for a week recovers; a row deleted on a count does not."""
        row = device(consecutive_failures=subscriptions.UNHEALTHY_AFTER)
        assert not row.healthy
        assert row.view()["healthy"] is False

    def test_a_malformed_device_is_refused_locally(self, home: Path) -> None:
        """Named as a property of the row, not blamed on the push service."""
        broken = Subscription(
            id="dev_x", endpoint="https://a.example/1", p256dh="not-a-key", auth="short"
        )
        with transport(always(201)) as client:
            result = delivery.send(broken, b"{}", key=load_or_create(home=home), client=client)
        assert result.outcome == "rejected"
        assert "could not encrypt" in (result.error or "")

    def test_one_bad_device_does_not_stop_the_others(self, home: Path) -> None:
        upsert(PROJECT, device("https://gone.example/1", id="dev_gone"), home=home)
        upsert(PROJECT, device("https://ok.example/2", id="dev_ok"), home=home)

        def by_host(request: httpx.Request) -> httpx.Response:
            return httpx.Response(410 if "gone" in str(request.url) else 201)

        result = self._round(fake_manager([waiting_task("task-001")]), home, by_host)
        assert (result.sent, result.pruned) == (1, 1)
        assert [row.id for row in load(PROJECT, home=home)] == ["dev_ok"]


# ----- the deliberate test push -----------------------------------------------------


class TestTheTestPush:
    def test_it_does_not_consume_the_episode(self, home: Path) -> None:
        """Otherwise proving that push works would cost the next real interruption."""
        upsert(PROJECT, device(), home=home)
        manager = fake_manager([waiting_task("task-001")])

        with transport(always(201)) as client:
            send_test(
                PROJECT,
                load(PROJECT, home=home)[0],
                home=home,
                client=client,
                key=load_or_create(home=home),
            )
        assert load(PROJECT, home=home)[0].last_episode_id is None

        with transport(always(201)) as client:
            result = delivery.deliver_round(
                manager, PROJECT, home=home, client=client, key=load_or_create(home=home)
            )
        assert result.sent == 1

    def test_a_dead_endpoint_found_by_a_test_is_forgotten(self, home: Path) -> None:
        upsert(PROJECT, device(), home=home)
        with transport(always(410)) as client:
            outcome = send_test(
                PROJECT,
                load(PROJECT, home=home)[0],
                home=home,
                client=client,
                key=load_or_create(home=home),
            )
        assert outcome.outcome == "gone"
        assert load(PROJECT, home=home) == ()


# ----- the shared rule --------------------------------------------------------------


class TestTheSharedRule:
    """`owes_notification` is the one policy, asked by the desktop and by the phone."""

    def _episode(self, **overrides: Any) -> Episode:
        base: Dict[str, Any] = {"id": "att_x", "started_at": NOW, "members": ("task-001",)}
        base.update(overrides)
        return Episode(**base)

    def test_no_episode_owes_nothing(self) -> None:
        assert owes_notification(None, None) is False

    def test_an_unseen_episode_is_owed(self) -> None:
        assert owes_notification(self._episode(), None) is True
        assert owes_notification(self._episode(), "att_older") is True

    def test_an_episode_this_client_has_drawn_is_not(self) -> None:
        assert owes_notification(self._episode(), "att_x") is False

    def test_an_acknowledged_episode_is_not(self) -> None:
        assert owes_notification(self._episode(acknowledged_at=NOW), None) is False


# ----- the watcher ------------------------------------------------------------------


class TestTheWatcher:
    """The loop that makes any of this work with no page open."""

    def _project(self, tmp_path: Path, home: Path, tasks: List[Task]) -> None:
        root = tmp_path / PROJECT
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        storage = task_store(root / "tasks", project_id=PROJECT)
        for task in tasks:
            storage.save_task(task)
        ProjectRegistry(home=home).add(root, project_id=PROJECT)

    def test_a_project_with_no_devices_is_skipped_entirely(
        self, tmp_path: Path, home: Path
    ) -> None:
        from agentjobs.push import watcher

        self._project(tmp_path, home, [waiting_task("task-001")])
        assert watcher.projects_with_devices(home) == []

    def test_it_delivers_once_and_then_goes_quiet(
        self, tmp_path: Path, home: Path, monkeypatch
    ) -> None:
        from agentjobs.push import watcher

        self._project(tmp_path, home, [waiting_task("task-001")])
        upsert(PROJECT, device(), home=home)
        assert [p.id for p in watcher.projects_with_devices(home)] == [PROJECT]

        sends: List[str] = []

        def fake_send(subscription, payload, **_kwargs):
            sends.append(subscription.id)
            return delivery.SendResult("sent", 201)

        monkeypatch.setattr(delivery, "send", fake_send)
        with server_process():
            first = watcher.poll_once(home)
            second = watcher.poll_once(home)

        assert [r.sent for r in first] == [1]
        assert [r.attempted for r in second] == [0]
        assert sends == ["dev_one"]

    def test_the_interval_is_overridable(self, monkeypatch) -> None:
        from agentjobs.push import watcher

        monkeypatch.setenv("AGENTJOBS_PUSH_POLL_SECONDS", "0.5")
        assert watcher.poll_interval() == 1.0  # floored, never a busy loop
        monkeypatch.setenv("AGENTJOBS_PUSH_POLL_SECONDS", "nonsense")
        assert watcher.poll_interval() == watcher.PUSH_POLL_SECONDS

    def test_nothing_is_watching_until_the_loop_runs(self) -> None:
        from agentjobs.push import watcher

        assert watcher.is_watching() is False


def test_the_state_lives_outside_every_checkout(home: Path) -> None:
    """Machine state about a person: not in the task store, not in a clone, not exported."""
    upsert(PROJECT, device(), home=home)
    path = subscriptions.subscriptions_path(PROJECT, home=home)
    assert path.parent == home / "push"
    assert path.exists()
