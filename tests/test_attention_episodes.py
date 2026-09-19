"""Attention episodes: one alert per episode, and what ends one (task-422).

The rule under test is on task-422 as a decision entry and in ``docs/attention.md``.
These are the cases that make it a rule rather than an intention:

* a burst of handoffs is **one** episode, so five tasks stopping at once owe one alert;
* a restart of the browser or of this server replays nothing, because the episode is
  already open and already recorded;
* clearing every human-ball task resets, and the next wait alerts again;
* an acknowledgment is a *deliberate* act, and a stale one is a no-op rather than an
  error;
* a run may not acknowledge, whatever credential it presents.

:func:`agentjobs.attention.advance` is tested directly as well as through the endpoint.
The transition is the whole of the policy and it is pure; asserting it through HTTP
alone would mean every case paid for a project fixture to make a point about a set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.attention import Episode, advance, episode_path, read_episode, reconcile
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
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
from support import task_store

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def waiting_task(task_id: str, *, title: str = "", priority: Priority = Priority.MEDIUM) -> Task:
    """A task stopped on a person: open, non-draft, ball on the human."""
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
        priority=priority,
        queue_position=int(task_id.split("-")[1]) * 100,
        category="general",
        spec=Spec(summary=f"Summary of {task_id}", description="Body."),
    )


class TestTheTransition:
    """The rule itself, as a function of the previous episode and the waiting set."""

    def test_nothing_waiting_is_no_episode(self) -> None:
        assert advance(None, [], now=NOW) is None

    def test_the_first_wait_opens_one(self) -> None:
        episode = advance(None, ["task-001"], now=NOW)

        assert episode is not None
        assert episode.members == ("task-001",)
        assert episode.started_at == NOW
        assert not episode.acknowledged

    def test_more_waits_join_an_unacknowledged_episode(self) -> None:
        """The anti-fatigue case. Five handoffs in a minute is one alert."""
        first = advance(None, ["task-001"], now=NOW)
        assert first is not None

        second = advance(first, ["task-001", "task-002", "task-003"], now=LATER)

        assert second is not None
        assert second.id == first.id, "a second id would be a second alert"
        assert second.members == ("task-001", "task-002", "task-003")
        assert second.started_at == NOW

    def test_polling_an_unchanged_waiting_set_changes_nothing(self) -> None:
        """The endpoint reconciles on every poll, so this is what keeps it quiet."""
        first = advance(None, ["task-001"], now=NOW)

        assert advance(first, ["task-001"], now=LATER) == first

    def test_clearing_everything_resets(self) -> None:
        open_episode = advance(None, ["task-001"], now=NOW)

        assert advance(open_episode, [], now=LATER) is None

    def test_a_new_wait_after_a_reset_alerts_again(self) -> None:
        first = advance(None, ["task-001"], now=NOW)
        assert advance(first, [], now=LATER) is None

        third = advance(None, ["task-002"], now=LATER)

        assert third is not None
        assert third.id != (first.id if first else None)

    def test_an_acknowledged_episode_re_arms_for_a_task_it_has_not_seen(self) -> None:
        acknowledged = Episode(
            id="att_first", started_at=NOW, members=("task-001",), acknowledged_at=NOW
        )

        after = advance(acknowledged, ["task-001", "task-002"], now=LATER)

        assert after is not None
        assert after.id != "att_first", "the person acted, so the next wait may interrupt"
        assert not after.acknowledged
        assert after.members == ("task-001", "task-002")

    def test_an_acknowledged_episode_stays_put_while_nothing_new_arrives(self) -> None:
        acknowledged = Episode(
            id="att_first",
            started_at=NOW,
            members=("task-001", "task-002"),
            acknowledged_at=NOW,
        )

        after = advance(acknowledged, ["task-001"], now=LATER)

        assert after is not None
        assert after.id == "att_first"
        assert after.acknowledged
        assert after.members == ("task-001",), "membership is the current set, so it shrinks"

    def test_a_task_that_returns_after_closing_is_new_attention(self) -> None:
        """Why membership shrinks rather than accumulating.

        A task handed back to the person, dealt with, and handed back again weeks
        later is a fresh reason to be interrupted. An episode that remembered every id
        it had ever held would silently swallow the second one.
        """
        acknowledged = Episode(
            id="att_first",
            started_at=NOW,
            members=("task-001", "task-002"),
            acknowledged_at=NOW,
        )
        after_close = advance(acknowledged, ["task-002"], now=LATER)
        assert after_close is not None

        returned = advance(after_close, ["task-001", "task-002"], now=LATER)

        assert returned is not None
        assert returned.id != "att_first"


def build_project(root: Path, tasks: List[Task]) -> None:
    """A registered project directory holding exactly the given tasks."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    storage = task_store(root / "tasks", project_id="inbox")
    for task in tasks:
        storage.save_task(task)


@pytest.fixture()
def client_for(tmp_path: Path, monkeypatch):
    """A one-project server whose attention state lives under a throwaway home."""

    def build(tasks: List[Task]) -> Tuple[TestClient, Path]:
        home = tmp_path / "home"
        monkeypatch.setenv("AGENTJOBS_HOME", str(home))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()

        build_project(tmp_path / "inbox", tasks)
        ProjectRegistry(home=home).add(tmp_path / "inbox", project_id="inbox")
        return TestClient(app), home

    yield build
    reset_dependency_cache()


def episode_of(client: TestClient) -> dict:
    """The episode the endpoint currently answers with."""
    return client.get("/api/projects/inbox/attention").json()["episode"]


class TestTheEndpoint:
    """What a client actually receives, and what a click does to it."""

    def test_an_empty_waiting_set_has_no_episode(self, client_for) -> None:
        client, _ = client_for([])

        assert episode_of(client) is None

    def test_the_episode_names_the_lead_task_so_a_toast_can(self, client_for) -> None:
        urgent = waiting_task("task-002", title="Urgent one", priority=Priority.HIGH)
        client, _ = client_for([waiting_task("task-001"), urgent])

        episode = episode_of(client)

        assert episode["lead_task_id"] == "task-002", "inbox order: priority first"
        assert episode["lead_task_title"] == "Urgent one"
        assert sorted(episode["tasks"]) == ["task-001", "task-002"]

    def test_polling_repeatedly_does_not_manufacture_a_second_episode(
        self, client_for
    ) -> None:
        """The reconcile lives on a GET, so this is the property that makes that safe."""
        client, _ = client_for([waiting_task("task-001")])

        ids = {episode_of(client)["id"] for _ in range(5)}

        assert len(ids) == 1

    def test_acknowledging_marks_the_episode_without_clearing_the_badge(
        self, client_for
    ) -> None:
        """Acknowledgment governs interruption; the indicator tracks the waiting set."""
        client, _ = client_for([waiting_task("task-001")])
        episode = episode_of(client)

        response = client.post(
            "/api/projects/inbox/attention/ack", json={"episode_id": episode["id"]}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["episode"]["acknowledged"] is True
        assert body["blocking"] == 1, "work is still stopped on the person"

    def test_a_stale_acknowledgment_is_a_no_op_rather_than_an_error(
        self, client_for
    ) -> None:
        """A click made one poll out of date races nothing and explains nothing."""
        client, _ = client_for([waiting_task("task-001")])
        current = episode_of(client)

        response = client.post(
            "/api/projects/inbox/attention/ack", json={"episode_id": "att_longgone"}
        )

        assert response.status_code == 200
        assert response.json()["episode"]["id"] == current["id"]
        assert response.json()["episode"]["acknowledged"] is False

    def test_a_service_restart_replays_nothing(self, client_for, tmp_path) -> None:
        """Cold start with work already waiting: the same episode, still known.

        The failure this rules out is the storm -- a fresh process deciding that every
        already-waiting task is news, which is what a memory-resident episode would do
        on every restart of the server.
        """
        client, home = client_for([waiting_task("task-001"), waiting_task("task-002")])
        first = episode_of(client)
        client.post("/api/projects/inbox/attention/ack", json={"episode_id": first["id"]})

        reset_dependency_cache()
        restarted = TestClient(app)
        after = episode_of(restarted)

        assert after["id"] == first["id"]
        assert after["acknowledged"] is True, "the person's act survived the restart"

    def test_the_state_is_a_file_outside_the_project(self, client_for, tmp_path) -> None:
        """It is machine state about a person, not project history.

        Asserted directly because the placement is the reason a clone of this
        repository does not arrive carrying somebody else's read receipts.
        """
        client, home = client_for([waiting_task("task-001")])
        episode_of(client)

        stored = episode_path("inbox", home=home)

        assert stored.is_file()
        assert home in stored.parents
        assert not list((tmp_path / "inbox").rglob("*.yaml.lock"))

    def test_a_corrupt_state_file_costs_one_alert_and_not_the_badge(
        self, client_for
    ) -> None:
        """Forgiving on purpose: the badge must not go down with the state file."""
        client, home = client_for([waiting_task("task-001")])
        first = episode_of(client)
        episode_path("inbox", home=home).write_text("not: [a, valid", encoding="utf-8")

        after = client.get("/api/projects/inbox/attention").json()

        assert after["blocking"] == 1
        assert after["episode"]["id"] != first["id"], "a lost episode re-alerts once"


class TestSeveralHandoffsAtOnce:
    """Nearly simultaneous handoffs are one episode, not a race between polls."""

    def test_two_concurrent_reconciles_agree_on_one_episode(
        self, client_for
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        client, _ = client_for([waiting_task("task-001"), waiting_task("task-002")])

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: client.get("/api/projects/inbox/attention").json(),
                    range(8),
                )
            )

        assert len({result["episode"]["id"] for result in results}) == 1

    def test_the_store_holds_the_same_episode_the_endpoint_reported(
        self, client_for
    ) -> None:
        client, home = client_for([waiting_task("task-001")])

        reported = episode_of(client)
        stored = read_episode("inbox", home=home)

        assert stored is not None
        assert stored.id == reported["id"]


class TestOnlyAPersonMayAcknowledge:
    """A run may not silence the alarm raised by its own handoff."""

    def test_the_route_requires_the_acknowledgment_capability(self) -> None:
        from agentjobs.api.authorization import ROUTE_CAPABILITIES
        from agentjobs.capabilities import GRANTS, Capability
        from agentjobs.principals import PrincipalKind

        rule = ROUTE_CAPABILITIES["acknowledge_attention"]

        assert rule.capability is Capability.ATTENTION_ACK
        assert Capability.ATTENTION_ACK not in GRANTS[PrincipalKind.RUN]
        assert Capability.ATTENTION_ACK in GRANTS[PrincipalKind.OWNER]


class TestReconcileAgreesWithTheBadge:
    """One predicate, asserted from the module a notification would call."""

    def test_reconcile_counts_what_the_badge_counts(self, client_for, tmp_path) -> None:
        from agentjobs.api.dependencies import manager_for
        from agentjobs.dashboard import count_blocking_human

        client, home = client_for([waiting_task("task-001"), waiting_task("task-002")])
        manager = manager_for(ProjectRegistry(home=home).get("inbox"))

        state = reconcile(manager, "inbox", home=home)

        assert state.blocking == count_blocking_human(manager) == 2
        assert sorted(task.id for task in state.waiting) == ["task-001", "task-002"]
