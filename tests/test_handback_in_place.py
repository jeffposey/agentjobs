"""Request Changes reaches a run that handed off and is still running (task-574).

The occurrence, task-563 and run_220c2439: the session handed its work to review with its
review sandbox running as a background job. Claude Code does not report a session with
work in flight as ``idle`` -- the ledger read ``busy``/``blocked`` -- so the poller read it
``RUNNING`` for as long as the sandbox ran. The owner clicked Request Changes, the
handback deferred to that live run "until it settles", and it never settled.

Everything here drives the production path: a real dispatched session (the fake Claude CLI
the runner and poller suites use), its real run lock and journal, and ``deliver_handback``
exactly as the route and the poller call it. The peer channel is the one seam replaced,
because what it would reach is a process that is not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.handback import (
    UNDELIVERABLE_REASON,
    deliver_handback,
    record_handback,
)
from agentjobs.dispatch.ledger import live_runs
from agentjobs.dispatch.peers import SESSIONS_DIR_ENV, PeerDelivery
from agentjobs.models_v2 import Ball, BallReason, LogEntryType
import test_approval_standdown
from test_approval_standdown import (
    APPROVER,
    dispatch_session,
    hand_off_for_review,
    set_ledger,
    the_task,
)
from test_dispatch_finish import DISPATCHABLE_CONFIG, make_session_dispatchable


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_approval_standdown``'s world -- clone, branch, task, skipping clock -- unchanged."""
    built: Dict[str, Any] = test_approval_standdown.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


FEEDBACK = "Make the button blue, then hand it back."

# The fake CLI's launch writes this row; the sandbox keeps it busy after the turn ends.
BUSY_WITH_A_SANDBOX: Dict[str, str] = {
    "id": "b55b35ad",
    "sessionId": "session_0142Vng",
    "kind": "background",
    "status": "busy",
    "state": "blocked",
}


def auto_dispatching(world: Dict[str, Any], tmp_path: Path) -> Path:
    """``make_session_dispatchable`` with auto-dispatch on, which a handback requires."""
    fake_cli = make_session_dispatchable(world, tmp_path)
    path = world["home"] / "dispatch.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["projects"]["demo"]["auto_dispatch"] = True
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return fake_cli


def register_live(session_uuid: str = "session_0142Vng") -> None:
    """The session's live-roster registration, shaped as Claude Code writes one."""
    directory = Path(os.environ[SESSIONS_DIR_ENV])
    (directory / "4242.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "sessionId": session_uuid,
                "jobId": session_uuid[:8],
                "cwd": "C:/projects/demo",
                "kind": "bg",
                "status": "busy",
                "name": "demo/task-001",
                "peerProtocol": 1,
                "messagingSocketPath": "//./pipe/LOCAL/cc-msg-deadbeef",
            }
        ),
        encoding="utf-8",
    )


def peer_channel(monkeypatch: pytest.MonkeyPatch, *, delivers: bool) -> List[str]:
    """Replace the peer send with a recorder that lands, or reports a miss."""
    sent: List[str] = []

    def send(target: Any, message: str, **_: Any) -> PeerDelivery:
        sent.append(message)
        if delivers:
            return PeerDelivery(True, "SendMessage delivered it", target.session_id)
        return PeerDelivery.missed("the message was held for approval", target.session_id)

    monkeypatch.setattr("agentjobs.dispatch.peers.send_peer_message", send)
    return sent


def request_changes(world: Dict[str, Any]) -> int:
    """What the Request Changes route writes: the ball to the agent, with the feedback."""
    task = world["manager"].handoff(
        world["task_id"],
        actor=APPROVER,
        ball=Ball.AGENT,
        ball_reason=BallReason.REVISE,
        ball_prompt=FEEDBACK,
        body="Changes requested.",
    )
    return int(task.log[-1].id)


def hand_back(world: Dict[str, Any]) -> Any:
    """Deliver and record, as the route does after its write."""
    task = the_task(world)
    outcome = deliver_handback(
        manager=world["manager"],
        project=world["project"],
        project_config=DISPATCHABLE_CONFIG,
        task=task,
        home=world["home"],
        api_base="http://127.0.0.1:1",
    )
    record_handback(world["manager"], task, outcome)
    return outcome


def live_run_ids(world: Dict[str, Any]) -> List[str]:
    return [record.run_id for record in live_runs(world["home"])]


def run_meta(world: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    path = world["home"] / "runs" / run_id / "meta.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def the_task563_shape(world: Dict[str, Any], tmp_path: Path) -> Any:
    """Dispatched, handed off for review, sandbox still running, then Request Changes."""
    fake_cli = auto_dispatching(world, tmp_path)
    handle = dispatch_session(world)
    hand_off_for_review(world)
    set_ledger(fake_cli, [BUSY_WITH_A_SANDBOX])
    register_live()
    # The review takes minutes, not the dispatcher's cooldown of none at all.
    dispatch_clock.sleep(300)
    return handle


class TestFeedbackReachesAHandedOffRunThatIsStillRunning:
    def test_it_is_delivered_in_place_and_the_record_says_so(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-1 and ac-2: on the code before task-574 this wrote `live_run_exists` and
        waited for a settle the sandbox would never allow."""
        handle = the_task563_shape(world, tmp_path)
        sent = peer_channel(monkeypatch, delivers=True)
        entry = request_changes(world)

        outcome = hand_back(world)

        assert outcome.reason == "delivered_in_place", outcome
        assert outcome.delivered and outcome.run_id == handle.run_id
        assert len(sent) == 1 and FEEDBACK in sent[0] and handle.run_id in sent[0]
        # Nothing rival started: the same run is still the task's one live run.
        assert live_run_ids(world) == [handle.run_id]
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.REVISE)
        notes = [e for e in task.log if (e.data or {}).get("handback_delivered")]
        assert len(notes) == 1 and notes[0].data["handback_delivered"] == "in_place"
        assert not [e for e in task.log if (e.data or {}).get("handback_refused")]
        meta = run_meta(world, handle.run_id)
        assert meta["delivered_through_entry"] == entry
        assert meta["handback_delivered_entry"] == entry
        assert meta.get("handback_pending") is None

    def test_the_revision_is_measured_from_the_delivery_not_the_first_handoff(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-3: a session that goes idle after the delivery without handing off again is
        not "completed" on the strength of the handoff the feedback answered."""
        fake_cli = auto_dispatching(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        set_ledger(fake_cli, [BUSY_WITH_A_SANDBOX])
        register_live()
        peer_channel(monkeypatch, delivers=True)
        request_changes(world)
        hand_back(world)
        # The sandbox is gone and the turn ended, with no second handoff.
        set_ledger(fake_cli, [{**BUSY_WITH_A_SANDBOX, "status": "idle", "state": "done"}])
        monkeypatch.setattr(
            "agentjobs.dispatch.runner.DispatchRunner.stop_session", lambda self, sid: True
        )
        from agentjobs.dispatch.poller import handle_from_record
        from agentjobs.dispatch.config import assert_dispatch_permitted
        from agentjobs.dispatch.runner import DispatchRunner

        record = next(r for r in live_runs(world["home"]) if r.run_id == handle.run_id)
        followed = handle_from_record(world["home"], record)
        assert followed is not None
        runner = DispatchRunner(
            manager=world["manager"],
            resolution=assert_dispatch_permitted("demo", world["home"]),
            project_root=world["root"],
            home=world["home"],
        )
        assert not runner._ball_moved(the_task(world), followed)

        # A second handoff after the delivery is the revision being handed back.
        hand_off_for_review(world)
        assert runner._ball_moved(the_task(world), followed)

    def test_a_miss_stands_the_run_down_and_a_fresh_run_carries_the_feedback(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = the_task563_shape(world, tmp_path)
        sent = peer_channel(monkeypatch, delivers=False)
        request_changes(world)

        outcome = hand_back(world)

        assert len(sent) == 1
        assert outcome.delivered, outcome
        assert outcome.run_id and outcome.run_id != handle.run_id
        assert run_meta(world, handle.run_id)["outcome"] == "completed"
        task = the_task(world)
        stood = [e for e in task.log if (e.data or {}).get("handback_delivered") == "stood_down"]
        assert len(stood) == 1
        dispatched = [
            e
            for e in task.log
            if e.type is LogEntryType.DISPATCH and e.data.get("run_id") == outcome.run_id
        ]
        assert len(dispatched) == 1
        assert live_run_ids(world) == [outcome.run_id]

    def test_when_neither_works_the_ball_goes_to_a_person_with_the_reason(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ac-3: never agent/revise with nobody acting on it."""
        handle = the_task563_shape(world, tmp_path)
        peer_channel(monkeypatch, delivers=False)
        monkeypatch.setattr(
            "agentjobs.dispatch.runner.DispatchRunner.stop_session", lambda self, sid: False
        )
        request_changes(world)

        outcome = hand_back(world)

        assert outcome.reason == UNDELIVERABLE_REASON, outcome
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert "did not reach an agent" in (task.ball_prompt or "")
        assert handle.run_id in (task.ball_prompt or "")
        assert live_run_ids(world) == [handle.run_id]

    def test_a_run_that_has_not_handed_off_is_still_left_alone(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A busy session that never handed off is working, and nothing is sent to it."""
        fake_cli = auto_dispatching(world, tmp_path)
        handle = dispatch_session(world)
        set_ledger(fake_cli, [{**BUSY_WITH_A_SANDBOX, "state": "working"}])
        register_live()
        sent = peer_channel(monkeypatch, delivers=True)
        request_changes(world)

        outcome = hand_back(world)

        assert outcome.reason == "live_run_exists", outcome
        assert sent == []
        assert live_run_ids(world) == [handle.run_id]
