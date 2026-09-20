"""A claimed task with nobody on it is found from the record alone (task-499).

The incident these cases are built from: task-421, 2026-09-19. An interactive session
supervising it hit its usage limit with a gate running, never handed off, and the task
read ``agent``/``revise`` for twenty-two hours while every surface AgentJobs has said an
agent was on it. Nothing caught it because every stall detector in ``dispatch/`` is keyed
on a run record, and that session had registered none.

So the properties under test are about what the *record* says, and each one is a way the
old detectors could be wrong:

* a task at ``agent``/``revise`` with no run record anywhere is reported -- the incident;
* the boundary holds in both directions, so a working session that is merely quiet is not;
* the ordinary backlog is not reported, though every task in it reads ``agent``;
* a deliberate hold is not reported, though it reads ``agent`` too;
* a live run doing its job is not reported, and one holding feedback it will never
  deliver is -- the 2026-09-20 case, where every run-side signal stayed healthy;
* the signal is derived and self-clearing: a log entry lands, the report stops, and the
  record is byte-for-byte what it was.

:mod:`agentjobs.stalled` is exercised directly as well as through the endpoint, for the
reason ``test_attention_episodes.py`` gives for testing ``advance`` directly: the
predicate is pure, and asserting all of it through HTTP would make every case pay for a
project fixture to make a point about a comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.config import load_dispatch_config
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.stalled import (
    NO_AGENT,
    UNDELIVERED_HANDBACK,
    StalledSettings,
    last_activity,
    load_settings,
    stall_for,
    stalled_in,
    stalls,
)
from support import task_store

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
"""The instant every case is judged at. Fixed, because a threshold measured against the
wall clock is a test that fails at midnight."""


class FakeRun:
    """A live run, as :class:`agentjobs.stalled.LiveRun` needs to see one.

    Two fields rather than a ``RunRecord``, which is the point of the protocol: the
    predicate's question about a run is "which one, and does it owe this task feedback",
    and a test that had to build a run directory to ask it would be testing the ledger.
    """

    def __init__(self, run_id: str, handback_pending: Optional[int] = None) -> None:
        self.run_id = run_id
        self.handback_pending = handback_pending


def _position(task_id: str) -> int:
    """A queue position derived from the id, because the store makes it unique per band."""
    digits = "".join(ch for ch in task_id if ch.isdigit()) or "1"
    return int(digits) * 100


def entry(
    minutes_ago: int,
    *,
    entry_id: int = 1,
    actor: str = "claude",
    base: datetime = NOW,
) -> LogEntry:
    """One log entry, written *minutes_ago* before *base*."""
    return LogEntry(
        id=entry_id,
        ts=base - timedelta(minutes=minutes_ago),
        actor=actor,
        type=LogEntryType.PROGRESS,
        body="Working.",
    )


def task(
    task_id: str = "task-421",
    *,
    lifecycle: Lifecycle = Lifecycle.ACTIVE,
    ball: Optional[Ball] = Ball.AGENT,
    ball_reason: Optional[BallReason] = BallReason.REVISE,
    quiet_minutes: int = 180,
    log: Optional[List[LogEntry]] = None,
    base: datetime = NOW,
) -> Task:
    """A task whose newest log entry is *quiet_minutes* before *base*.

    *base* is :data:`NOW` for the predicate's own cases, which judge at a fixed instant,
    and the real clock for the cases that go through the endpoint -- where the reconcile
    reads the wall clock and a fixed timestamp would make every task hours stale.
    """
    closed = lifecycle is Lifecycle.CLOSED
    claimed = lifecycle is Lifecycle.ACTIVE
    return Task(
        id=task_id,
        assignment=Assignment(owner="claude" if claimed else None),
        title=f"Title of {task_id}",
        created=base - timedelta(days=2),
        updated=base - timedelta(minutes=quiet_minutes),
        lifecycle=lifecycle,
        ball=None if closed else ball,
        ball_reason=None if closed else ball_reason,
        ball_prompt=None if closed else "Address the review feedback.",
        outcome=Outcome.COMPLETED if closed else None,
        priority=Priority.HIGH,
        queue_position=None if closed else _position(task_id),
        category="general",
        spec=Spec(summary=f"Summary of {task_id}", description="Body."),
        log=log if log is not None else [entry(quiet_minutes, base=base)],
    )


def judge(
    record: Task,
    run: Optional[FakeRun] = None,
    *,
    minutes: int = 60,
    handback_minutes: int = 30,
):
    """The verdict on one task at :data:`NOW`."""
    return stall_for(record, run, minutes=minutes, handback_minutes=handback_minutes, now=NOW)


class TestTheIncident:
    """task-421 itself, reconstructed from what the record said at the time."""

    def test_a_task_at_agent_revise_with_no_run_at_all_is_reported(self) -> None:
        """The whole finding: the state says an agent is on this, and none is.

        No run record is not a missing input here -- it is the input. The session that
        held this task registered nothing, so a detector keyed on a run has nothing to
        look at, and this one is asked to reach a verdict from that absence.
        """
        stall = judge(task(quiet_minutes=22 * 60))

        assert stall is not None
        assert stall.reason == NO_AGENT
        assert stall.run_id == ""
        assert stall.quiet_seconds == pytest.approx(22 * 3600)
        assert stall.describe() == "task-421: no agent on it, quiet for 22h 0m"

    def test_the_quiet_is_measured_from_the_newest_log_entry(self) -> None:
        """Not from ``updated``, which an edit to a field moves without anybody working."""
        record = task(
            quiet_minutes=200,
            log=[entry(400, entry_id=1), entry(90, entry_id=2), entry(200, entry_id=3)],
        )

        assert last_activity(record) == NOW - timedelta(minutes=90)
        assert judge(record) is not None, "90 minutes is past the hour"

    def test_a_task_with_no_log_at_all_falls_back_to_when_it_was_created(self) -> None:
        record = task(log=[])

        assert last_activity(record) == record.created
        assert judge(record) is not None


class TestTheBoundary:
    """A working session is never reported; a stopped one is. The line between them."""

    def test_a_quiet_working_session_inside_the_threshold_is_not_reported(self) -> None:
        """Fifty-nine minutes of silence is a gate and a long refactor, not an outage."""
        assert judge(task(quiet_minutes=59)) is None

    def test_one_minute_later_it_is(self) -> None:
        assert judge(task(quiet_minutes=61)) is not None

    def test_exactly_at_the_threshold_it_is(self) -> None:
        """``>=``, stated as a case so the comparison cannot drift silently."""
        stall = judge(task(quiet_minutes=60))

        assert stall is not None
        assert stall.threshold_seconds == 3600

    def test_the_threshold_is_the_one_it_is_given(self) -> None:
        """Raising it moves the line, which is what makes it worth configuring."""
        assert judge(task(quiet_minutes=61), minutes=120) is None
        assert judge(task(quiet_minutes=121), minutes=120) is not None


class TestWhatIsNotAStall:
    """Three states that read ``agent`` and mean nobody should be doing anything."""

    def test_the_backlog_is_not_an_outage(self) -> None:
        """Every unclaimed task in the queue is ``ready``/``agent``/``available``.

        Measured on this repository's corpus on 2026-09-20: counting them turns 3,101
        agent-held quiet stretches into 4,617 and the 90th percentile from 16 minutes
        into 67, because the oldest of them have been sitting there for weeks.
        """
        backlog = task(
            lifecycle=Lifecycle.READY,
            ball_reason=BallReason.AVAILABLE,
            quiet_minutes=40 * 24 * 60,
        )

        assert judge(backlog) is None

    def test_a_deliberate_hold_is_not_an_outage(self) -> None:
        """A hold has a release condition on the record; nobody is meant to be on it."""
        assert judge(task(ball_reason=BallReason.HOLD, quiet_minutes=18 * 60)) is None

    def test_a_closed_task_is_not_an_outage(self) -> None:
        assert judge(task(lifecycle=Lifecycle.CLOSED)) is None

    def test_a_task_a_person_holds_is_not_this_signal(self) -> None:
        """It is already in the waiting set on its own terms; this must not double-count."""
        held = task(ball=Ball.HUMAN, ball_reason=BallReason.REVIEW, quiet_minutes=600)

        assert judge(held) is None

    def test_a_live_run_with_nothing_pending_is_working(self) -> None:
        """Whether *that* has hung is a question the run-keyed detectors already own."""
        assert judge(task(quiet_minutes=600), FakeRun("run_alive")) is None


class TestTheUndeliveredHandback:
    """The 2026-09-20 case: a live run, healthy by every run-side signal, not going to act.

    task-176-dispatch-on-create handed off for review; the owner requested changes; the
    dispatcher correctly declined to start a second run and recorded that the feedback
    would be delivered when the run settled. The run never settled. Asking it whether it
    was alive got yes and learned nothing.
    """

    def test_a_live_run_holding_feedback_it_never_delivered_is_reported(self) -> None:
        stall = judge(task(quiet_minutes=40), FakeRun("run_9df54f12", handback_pending=12))

        assert stall is not None
        assert stall.reason == UNDELIVERED_HANDBACK
        assert stall.run_id == "run_9df54f12"
        assert "run_9df54f12" in stall.describe()

    def test_it_uses_the_shorter_threshold(self) -> None:
        """The record names the run the feedback is addressed to, so this is a delivery
        failure rather than an absence, and a person has just clicked something."""
        pending = FakeRun("run_9df54f12", handback_pending=12)

        assert judge(task(quiet_minutes=29), pending) is None
        assert judge(task(quiet_minutes=31), pending) is not None

    def test_an_agent_taking_the_feedback_in_its_own_time_is_not_reported(self) -> None:
        assert judge(task(quiet_minutes=20), FakeRun("run_x", handback_pending=12)) is None


class TestTheSet:
    """What a caller gets for a whole corpus."""

    def test_the_quietest_is_first(self) -> None:
        found = stalls(
            [
                task("task-a", quiet_minutes=90),
                task("task-b", quiet_minutes=22 * 60),
                task("task-c", quiet_minutes=10),
            ],
            {},
            minutes=60,
            handback_minutes=30,
            now=NOW,
        )

        assert [stall.task_id for stall in found] == ["task-b", "task-a"]

    def test_no_runs_in_hand_is_an_answer_and_not_a_failure(self) -> None:
        """A machine that has never dispatched still has tasks that can be abandoned."""
        assert stalls([task()], {}, minutes=60, handback_minutes=30, now=NOW)


class TestTheThresholdIsConfigurable:
    """a6: the numbers are settings, and the defaults are the measured ones."""

    def test_the_defaults_are_sixty_and_thirty(self) -> None:
        assert StalledSettings() == StalledSettings(enabled=True, minutes=60, handback_minutes=30)

    def test_a_machine_with_no_dispatch_config_gets_the_defaults(self, tmp_path: Path) -> None:
        assert load_settings(tmp_path) == StalledSettings()

    def test_the_block_is_read_from_dispatch_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "dispatch.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "stalled_tasks": {
                        "enabled": True,
                        "minutes": 90,
                        "handback_minutes": 10,
                    },
                }
            ),
            encoding="utf-8",
        )

        config = load_dispatch_config(tmp_path)

        assert config is not None
        assert config.stalled_tasks.minutes == 90
        assert config.stalled_tasks.handback_minutes == 10
        assert "stalled_tasks.minutes" in config.explicit_keys
        assert load_settings(tmp_path).minutes == 90

    def test_switching_it_off_reports_nothing(self, tmp_path: Path) -> None:
        off = StalledSettings(enabled=False)

        assert stalled_in([task()], project_id="inbox", now=NOW, settings=off) == []


# ----- through the endpoint -----------------------------------------------------------


def build_project(root: Path, tasks: List[Task]) -> None:
    """A registered project directory holding exactly the given tasks."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Inbox", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    storage = task_store(root / "tasks", project_id="inbox")
    for record in tasks:
        storage.save_task(record)


@pytest.fixture()
def client_for(tmp_path: Path, monkeypatch):
    """A one-project server whose home holds the attention state and the run ledger."""

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


def live_run(home: Path, run_id: str, task_id: str, *, handback_pending: int | None = None) -> None:
    """A run directory every reader in the system treats as live.

    ``batch`` rather than ``session``, for the reason ``test_dispatch_handback.py`` gives:
    a session run is one other paths try to poll, which shells out to a session manager
    that is not here.
    """
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": "inbox",
        "mode": "batch",
        "status": "running",
        "started_at": "2026-09-20T12:00:00+00:00",
    }
    if handback_pending is not None:
        meta["handback_pending"] = handback_pending
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")


def now_task(task_id: str = "task-421", **kwargs: Any) -> Task:
    """A task built against the wall clock, for the cases that go through the endpoint.

    ``reconcile`` runs on a GET and reads the real clock, so a record pinned to
    :data:`NOW` would be hours stale by the time the server judged it -- which is the
    difference between asserting a threshold and asserting what time it is.
    """
    return task(task_id, base=datetime.now(timezone.utc), **kwargs)


def attention_of(client: TestClient) -> Dict[str, Any]:
    payload: Dict[str, Any] = client.get("/api/projects/inbox/attention").json()
    return payload


class TestItReachesTheOwner:
    """a3: through the episode that already exists, not through a path of its own."""

    def test_a_stalled_task_opens_an_attention_episode(self, client_for) -> None:
        client, _ = client_for([now_task(quiet_minutes=22 * 60)])

        payload = attention_of(client)

        assert payload["blocking"] == 1
        assert payload["episode"]["tasks"] == ["task-421"]
        assert payload["episode"]["lead_task_id"] == "task-421"
        assert payload["stalled"][0]["reason"] == NO_AGENT

    def test_it_obeys_the_one_alert_rule_like_anything_else_in_the_set(self, client_for) -> None:
        """Acknowledging it is the same act, on the same episode. No second policy."""
        client, _ = client_for([now_task(quiet_minutes=22 * 60)])
        episode = attention_of(client)["episode"]

        client.post("/api/projects/inbox/attention/ack", json={"episode_id": episode["id"]})
        after = attention_of(client)

        assert after["episode"]["id"] == episode["id"]
        assert after["episode"]["acknowledged"] is True
        assert after["blocking"] == 1, "acknowledgment governs interruption, not the badge"

    def test_the_deep_link_reaches_a_list_that_holds_the_whole_set(self, client_for) -> None:
        """`status=human` would have landed a person on a list shorter than the number."""
        client, _ = client_for(
            [
                now_task("task-421", quiet_minutes=22 * 60),
                now_task(
                    "task-430",
                    ball=Ball.HUMAN,
                    ball_reason=BallReason.REVIEW,
                    quiet_minutes=5,
                ),
            ]
        )

        payload = attention_of(client)

        assert payload["blocking"] == 2
        assert "status=attention" in payload["episode"]["deep_link"]

    def test_a_working_session_is_absent_from_the_whole_answer(self, client_for) -> None:
        client, _ = client_for([now_task(quiet_minutes=20)])

        payload = attention_of(client)

        assert payload["blocking"] == 0
        assert payload["episode"] is None
        assert payload["stalled"] == []

    def test_a_live_run_against_the_task_keeps_it_out(self, client_for) -> None:
        client, home = client_for([now_task(quiet_minutes=22 * 60)])
        live_run(home, "run_working", "task-421")

        assert attention_of(client)["blocking"] == 0

    def test_a_live_run_holding_undelivered_feedback_does_not(self, client_for) -> None:
        client, home = client_for([now_task(quiet_minutes=22 * 60)])
        live_run(home, "run_9df54f12", "task-421", handback_pending=12)

        payload = attention_of(client)

        assert payload["stalled"][0]["reason"] == UNDELIVERED_HANDBACK
        assert payload["stalled"][0]["run_id"] == "run_9df54f12"

    def test_the_dashboard_panel_counts_what_the_badge_counts(self, client_for) -> None:
        """One predicate, one order -- the property ``test_attention_tiers.py`` is about."""
        client, _ = client_for([now_task(quiet_minutes=22 * 60)])

        dashboard = client.get("/api/projects/inbox/dashboard").json()

        assert dashboard["next_action"] == "blocked"
        assert [row["id"] for row in dashboard["waiting_tasks"]] == ["task-421"]
        assert dashboard["stalled"][0]["task_id"] == "task-421"
        assert dashboard["stats"]["waiting_for_human"] == 1
        assert attention_of(client)["blocking"] == len(dashboard["waiting_tasks"])


class TestItIsDerivedAndSelfClearing:
    """a2: nothing is written to mark a stall, and nothing has to be retracted."""

    def test_reporting_one_writes_nothing_to_the_task(self, client_for) -> None:
        client, _ = client_for([now_task(quiet_minutes=22 * 60)])
        before = client.get("/api/projects/inbox/tasks/task-421").json()

        assert attention_of(client)["blocking"] == 1
        after = client.get("/api/projects/inbox/tasks/task-421").json()

        assert after["ball"] == before["ball"] == "agent"
        assert after["ball_reason"] == before["ball_reason"] == "revise"
        assert after["ball_prompt"] == before["ball_prompt"]
        assert len(after["log"]) == len(before["log"])
        assert after["updated"] == before["updated"]

    def test_a_log_entry_landing_stops_the_report_with_nothing_to_retract(self, client_for) -> None:
        """The reason this is derived rather than written: the agent coming back is the
        whole of the repair, and no ball anybody wrote has to be taken off."""
        client, _ = client_for([now_task(quiet_minutes=22 * 60)])
        assert attention_of(client)["blocking"] == 1

        client.post(
            "/api/projects/inbox/tasks/task-421/log",
            json={
                "actor": "claude",
                "type": "progress",
                "body": "Back on it; the gate is green.",
            },
        )

        after = attention_of(client)
        assert after["blocking"] == 0
        assert after["stalled"] == []
        assert after["episode"] is None
