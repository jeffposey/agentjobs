"""A stopped finish reaches a live run that is waiting behind its review handoff (task-569).

The occurrence, task-368 and fin_8cf34319 on 2026-09-24: the run handed off for review with
its review sandbox running as a background job, the approval's finish went red at the
gate, and the escalation deferred to the live run (``live_run``). Nothing delivered the
escalation to it and the sandbox kept it from ever ending, so the task sat at
``agent``/``work`` with nobody acting until the owner messaged the session.

Everything here drives the production path: a real dispatched session on the fake Claude
CLI, a real branch whose gate goes red, and ``finish_task`` itself. The peer channel is
the one seam replaced, because what it would reach is a process that is not here, and
``stop_session`` where a test needs the stop to fail or to land.
"""

from __future__ import annotations

import json

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.escalation_takeover import (
    STOOD_DOWN,
    UNREACHABLE,
    WOKEN,
    human_turn_since,
)
from agentjobs.dispatch.finish import ESCALATION_PENDING
from agentjobs.dispatch.ledger import live_runs
from agentjobs.models_v2 import Ball, BallReason, LogEntryType
import test_approval_standdown
from test_approval_standdown import hand_off_for_review, set_ledger, the_task
from test_dispatch_finish import (
    approve,
    break_the_gate,
    make_session_dispatchable,
    run,
    someone_is_actually_there,
    start_live_session,
)
from test_handback_in_place import BUSY_WITH_A_SANDBOX, peer_channel, register_live


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_approval_standdown``'s world: clone, branch, task and a skipping clock."""
    built: Dict[str, Any] = test_approval_standdown.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


SESSION_UUID = "b55b35ad-0000-4000-8000-000000000001"
"""The fake CLI's session, under the full uuid its transcript is named for."""


def the_task368_shape(world: Dict[str, Any], tmp_path: Path) -> Any:
    """Dispatched, handed off for review with a sandbox still running, approved, gate red."""
    fake_cli = make_session_dispatchable(world, tmp_path)
    handle = start_live_session(world, caused_by=1)
    hand_off_for_review(world)
    set_ledger(fake_cli, [{**BUSY_WITH_A_SANDBOX, "sessionId": SESSION_UUID}])
    register_live(SESSION_UUID)
    # The review takes minutes, not the dispatcher's cooldown of none at all.
    dispatch_clock.sleep(300)
    approve(world)
    break_the_gate(world)
    world["fake_cli"] = fake_cli
    return handle


def review_handoff_ts(world: Dict[str, Any]) -> Any:
    return next(
        entry.ts
        for entry in reversed(the_task(world).log)
        if entry.type is LogEntryType.HANDOFF and entry.actor == "claude"
    )


def write_transcript(
    world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, person_typed: bool
) -> None:
    """The session's JSONL, as Claude Code writes it, after its review handoff.

    Always carries the tool result every session writes straight after its handoff call
    returns, and the reply after it: both are ``user``/``assistant`` records that are not
    a person, and the stand-down must not read them as one.
    """
    handed = review_handoff_ts(world)
    home = tmp_path / "claude-home-569"
    folder = home / "projects" / "C--projects-demo"
    folder.mkdir(parents=True)
    records: List[Dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": SESSION_UUID,
            "timestamp": (handed + timedelta(seconds=1)).isoformat(),
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        },
        {
            "type": "assistant",
            "sessionId": SESSION_UUID,
            "timestamp": (handed + timedelta(seconds=3)).isoformat(),
            "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Done."}]},
        },
    ]
    if person_typed:
        records.append(
            {
                "type": "user",
                "sessionId": SESSION_UUID,
                "timestamp": (handed + timedelta(seconds=60)).isoformat(),
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": "Leave the sandbox up, I'm testing."},
            }
        )
    (folder / f"{SESSION_UUID}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv(CLAUDE_HOME_ENV, str(home))


def stop_lands(monkeypatch: pytest.MonkeyPatch, fake_cli: Path, *, lands: bool) -> List[str]:
    """Replace the driver's stop. A stop that lands takes the session out of the ledger."""
    stopped: List[str] = []

    def stop(self: Any, session_id: str) -> bool:
        stopped.append(session_id)
        if lands:
            set_ledger(fake_cli, [])
        return lands

    monkeypatch.setattr("agentjobs.dispatch.runner.DispatchRunner.stop_session", stop)
    return stopped


def run_meta(world: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    path = world["home"] / "runs" / run_id / "meta.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def finish_meta(result: Any) -> Dict[str, Any]:
    return dict(yaml.safe_load((result.directory / "meta.yaml").read_text(encoding="utf-8")))


def takeover_notes(world: Dict[str, Any]) -> List[Optional[str]]:
    return [
        (entry.data or {}).get("escalation_takeover")
        for entry in the_task(world).log
        if (entry.data or {}).get("escalation_takeover")
    ]


def live_run_ids(world: Dict[str, Any]) -> List[str]:
    return [record.run_id for record in live_runs(world["home"])]


class TestAStoppedFinishReachesAnIdleRun:
    def test_it_wakes_the_session_in_place(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ac-1 and ac-3: on the code before task-569 this recorded `live_run` and waited
        for a run the sandbox would never let end."""
        handle = the_task368_shape(world, tmp_path)
        sent = peer_channel(monkeypatch, delivers=True)
        stopped = stop_lands(monkeypatch, world["fake_cli"], lands=True)

        result = run(world)

        assert result.escalation_dispatch == WOKEN, result
        assert result.dispatched_run_id == handle.run_id
        assert finish_meta(result)["escalation_dispatch"] == WOKEN
        assert len(sent) == 1 and handle.run_id in sent[0]
        assert "stopped at `gate`" in sent[0], "the escalation's own prompt is the payload"
        assert stopped == [], "a session that was woken is not also stopped"
        assert live_run_ids(world) == [handle.run_id]
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.WORK)
        assert takeover_notes(world) == [WOKEN]
        meta = run_meta(world, handle.run_id)
        # Measured from the wake, and still re-asked if the woken run ends without acting.
        escalation = max(
            e.id for e in task.log if e.type is LogEntryType.HANDOFF and e.actor == "finisher"
        )
        assert meta["handback_delivered_entry"] == escalation
        assert meta[ESCALATION_PENDING] == result.finish_id

    def test_a_miss_stands_the_run_down_and_dispatches_the_repair(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handle = the_task368_shape(world, tmp_path)
        write_transcript(world, tmp_path, monkeypatch, person_typed=False)
        peer_channel(monkeypatch, delivers=False)
        stopped = stop_lands(monkeypatch, world["fake_cli"], lands=True)

        result = run(world)

        assert result.escalation_dispatch == STOOD_DOWN, the_task(world).ball_prompt
        assert finish_meta(result)["escalation_dispatch"] == STOOD_DOWN
        assert stopped == [handle.session_id]
        assert run_meta(world, handle.run_id)["outcome"] == "completed"
        assert result.dispatched_run_id and result.dispatched_run_id != handle.run_id
        assert live_run_ids(world) == [result.dispatched_run_id]
        assert takeover_notes(world) == [STOOD_DOWN]
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.WORK)
        someone_is_actually_there(world)

    def test_a_session_somebody_typed_into_is_never_stopped(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The constraint: a person talking to it is not an idle session."""
        handle = the_task368_shape(world, tmp_path)
        write_transcript(world, tmp_path, monkeypatch, person_typed=True)
        peer_channel(monkeypatch, delivers=False)
        stopped = stop_lands(monkeypatch, world["fake_cli"], lands=True)

        result = run(world)

        assert result.escalation_dispatch == UNREACHABLE, result
        assert stopped == []
        assert live_run_ids(world) == [handle.run_id]
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert "typed into it" in (task.ball_prompt or "")

    def test_an_unreadable_transcript_is_not_a_licence_to_stop(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handle = the_task368_shape(world, tmp_path)
        peer_channel(monkeypatch, delivers=False)
        stopped = stop_lands(monkeypatch, world["fake_cli"], lands=True)

        result = run(world)

        assert result.escalation_dispatch == UNREACHABLE, result
        assert stopped == []
        assert live_run_ids(world) == [handle.run_id]
        assert the_task(world).ball is Ball.HUMAN

    def test_when_neither_works_the_ball_goes_to_a_person_in_the_same_finish(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ac-2: never agent/work with no agent behind it."""
        handle = the_task368_shape(world, tmp_path)
        write_transcript(world, tmp_path, monkeypatch, person_typed=False)
        peer_channel(monkeypatch, delivers=False)
        stop_lands(monkeypatch, world["fake_cli"], lands=False)

        result = run(world)

        assert result.escalation_dispatch == UNREACHABLE, result
        assert finish_meta(result)["escalation_dispatch"] == UNREACHABLE
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert "no agent was started" in (task.ball_prompt or "")
        assert handle.run_id in (task.ball_prompt or "")
        assert live_run_ids(world) == [handle.run_id]

    def test_a_run_that_has_not_handed_off_is_still_deferred_to(
        self,
        world: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live run whose last move was not a review handoff may be acting: unchanged."""
        make_session_dispatchable(world, tmp_path)
        handle = start_live_session(world, caused_by=1)
        approve(world)
        break_the_gate(world)
        sent = peer_channel(monkeypatch, delivers=True)

        result = run(world)

        assert result.escalation_dispatch == "live_run", result
        assert sent == []
        assert live_run_ids(world) == [handle.run_id]


class TestHumanTurnSince:
    def test_only_a_persons_own_turn_counts(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_session_dispatchable(world, tmp_path)
        start_live_session(world, caused_by=1)
        hand_off_for_review(world)
        write_transcript(world, tmp_path, monkeypatch, person_typed=False)
        assert human_turn_since("b55b35ad", review_handoff_ts(world)) is False

    def test_no_transcript_is_unknowable(self) -> None:
        from datetime import datetime, timezone

        assert human_turn_since("0badc0de", datetime.now(timezone.utc)) is None
