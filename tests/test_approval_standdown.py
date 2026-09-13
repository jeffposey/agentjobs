"""An approval reaches a merge while the session that asked for it is still attached (task-312).

The incident, on task-021, task-022, task-292 and task-294: a dispatched session handed its
work to review and stayed attached, holding the task's run lock. The human approved; the
scripted finish lost the lock to that live run and declined ``locked``. Nothing merged and
nothing said so. The only way on was a person cancelling the run -- whose cancellation
then wrote *nobody was told what this task needs* over the approval.

Everything here runs the production path against real things: a real git clone with a
branch in a worktree, a real dispatched session (the fake Claude CLI the runner and poller
suites drive), a real run lock, the real execution journal and the real poller. Nothing
reaches into a run's record to make a state true. The four sessions of the incident are
the four ways a session can still be attached when the approval lands: busy, idle but not
yet polled, stuck on an expired login, and refusing to stop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml

from agentjobs.api.routes.tasks import APPROVAL_CLEARANCE
from agentjobs.dispatch.approval import (
    accept_signals,
    approval_data,
    human_handoffs_since,
    reviewed_branches,
    source_event,
    standing_approval_for,
    supersede_earlier_approvals,
)
from agentjobs.dispatch.finish import DECLINED, FINISHED, finish_task
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.journal import journal, task_feed
from agentjobs.dispatch.ledger import (
    DispatchLedger,
    StopResult,
    live_runs,
    read_task_lock_holder,
)
from agentjobs.dispatch.wake import build_wake_prompt
from agentjobs.execution.coordinator import import_source_events, task_feed_source
from agentjobs.execution.reducer import (
    Event,
    S_STANDING_DOWN,
    S_STOPPING,
    next_intents,
    replay,
)
from agentjobs.execution.store import ExecutionStore, SourceEvent
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from test_dispatch_finish import (  # noqa: F401 - `world` is a fixture
    DISPATCHABLE_CONFIG,
    head,
    landed,
    make_session_dispatchable,
    merged_into,
    settings,
)
import test_dispatch_finish
import test_dispatch_handback

APPROVER = "Jeff Posey"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """``test_dispatch_finish``'s real clone, branch, worktree and task, reused unchanged."""
    built: Dict[str, Any] = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    return built


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """``test_dispatch_handback``'s served project and throwaway home, reused unchanged."""
    yield from test_dispatch_handback.served.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )


# ----- the sequence, as it happens --------------------------------------------


def dispatch_session(world: Dict[str, Any]) -> Any:
    """A session dispatched the way a person's click dispatches one: lock, journal and all."""
    manager = world["manager"]
    note = (
        manager.add_log_entry(
            world["task_id"], actor=APPROVER, type=LogEntryType.NOTE, body="Work this."
        )
        .log[-1]
        .id
    )
    return dispatch_task(
        manager=manager,
        project=world["project"],
        project_config=DISPATCHABLE_CONFIG,
        request=DispatchRequest(task_id=world["task_id"], caused_by=note),
        home=world["home"],
        api_base="http://127.0.0.1:1",
    )


def hand_off_for_review(world: Dict[str, Any]) -> None:
    world["manager"].handoff(
        world["task_id"],
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Built and verified; please review.",
    )


def approve(world: Dict[str, Any], note: str = "") -> int:
    """Approve exactly as the route does: clearance, receipt, and the synchronous accept."""
    manager = world["manager"]
    task = manager.get_task(world["task_id"])
    approved = manager.handoff(
        world["task_id"],
        actor=APPROVER,
        ball=Ball.AGENT,
        ball_reason=BallReason.WORK,
        ball_prompt=APPROVAL_CLEARANCE,
        body=f"Approved by {APPROVER} through the web UI.",
        data=approval_data(
            approver=APPROVER, note=note, reviewed=reviewed_branches(world["root"], task)
        ),
    )
    accept_signals(world["home"], "demo", approved, [approved.log[-1]])
    return int(approved.log[-1].id)


def finish(world: Dict[str, Any]) -> Any:
    return finish_task(
        manager=world["manager"],
        project=world["project"],
        task_id=world["task_id"],
        approver=APPROVER,
        home=world["home"],
        api_base="http://127.0.0.1:1",
        settings=settings(),
    )


def set_ledger(fake_cli: Path, rows: List[Dict[str, str]]) -> None:
    (fake_cli.parent / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")


def the_task(world: Dict[str, Any]) -> Any:
    task = world["manager"].get_task(world["task_id"])
    assert task is not None
    return task


def result_for(world: Dict[str, Any], run_id: str) -> Any:
    results = [
        entry
        for entry in the_task(world).log
        if entry.type is LogEntryType.DISPATCH_RESULT and entry.data.get("run_id") == run_id
    ]
    assert len(results) == 1, results
    return results[0]


def prompts_written(world: Dict[str, Any]) -> List[str]:
    return [
        entry.body or ""
        for entry in the_task(world).log
        if entry.type is LogEntryType.HANDOFF and entry.actor != APPROVER
    ]


def events_of(world: Dict[str, Any], run_id: str) -> List[str]:
    store = journal(world["home"])
    attempt = store.attempt(run_id)
    assert attempt is not None and attempt.execution_id
    return [event.kind for event in store.events(attempt.execution_id)]


def inbox_row(world: Dict[str, Any], entry_id: int) -> Any:
    task = the_task(world)
    entry = next(item for item in task.log if item.id == entry_id)
    event = source_event("demo", task.id, entry)
    row = journal(world["home"]).signal(task_feed_source("demo"), event.source_event_id)
    assert row is not None
    return row


# ----- sc-2 and sc-4: approve while the session is alive ------------------------


class TestApprovingWhileTheSessionIsStillAttached:
    """The four states the incident's runs were in when the approval landed."""

    def test_a_busy_session_stands_down_and_the_approval_merges(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """task-021's shape, end to end: approve, and the merge happens with no second act."""
        make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approval = approve(world)
        holder = read_task_lock_holder(world["home"], world["task_id"], project_id="demo")
        assert holder is not None and holder.run_id == handle.run_id, "the precondition"

        result = finish(world)

        assert result.outcome == FINISHED, result.render()
        assert landed(world["root"], result)
        assert the_task(world).lifecycle is Lifecycle.CLOSED
        ended = result_for(world, handle.run_id)
        assert ended.data["outcome"] == "completed"
        assert "Stood down" in (ended.body or "")
        assert all("nobody was told" not in prompt for prompt in prompts_written(world))
        # durable-3: an internal transfer is never recorded as a cancellation.
        attempt = journal(world["home"]).attempt(handle.run_id)
        assert attempt is not None and attempt.cancel_requested is False
        kinds = events_of(world, handle.run_id)
        assert "stand_down_requested" in kinds and "stop_requested" not in kinds
        assert inbox_row(world, approval).status == "consumed"

    def test_an_idle_session_not_yet_polled_is_settled_rather_than_waited_for(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task-292 and task-294: idle, and the poller had simply not looked yet."""
        fake_cli = make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approve(world)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        stops: List[str] = []

        def stop_session(self: Any, session_id: str) -> bool:
            stops.append(session_id)
            return True

        monkeypatch.setattr("agentjobs.dispatch.runner.DispatchRunner.stop_session", stop_session)

        result = finish(world)

        assert result.outcome == FINISHED, result.render()
        # Settled by the first poll: the only `stop` is the settle's own reap of a finished
        # session. A stand-down that also sent its own stop would make this two.
        assert stops == ["b55b35ad"]
        assert result_for(world, handle.run_id).data["outcome"] == "completed"

    def test_a_session_stuck_on_an_expired_login_is_still_taken_over(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task-022: the auth-stall check used to hold the run live on every poll, for ever."""
        make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approve(world)
        monkeypatch.setattr(
            "agentjobs.dispatch.runner.DispatchRunner.auth_stall", lambda self, handle: object()
        )

        result = finish(world)

        assert result.outcome == FINISHED, result.render()
        assert result_for(world, handle.run_id).data["outcome"] == "completed"

    def test_a_session_that_will_not_stop_merges_nothing_until_the_poller_sees_it_end(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quiescence before transfer: failing to stop is still stopping, and never a merge."""
        fake_cli = make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approve(world)
        monkeypatch.setattr(
            "agentjobs.dispatch.runner.DispatchRunner.stop_session", lambda self, session_id: False
        )
        monkeypatch.setattr("agentjobs.dispatch.standdown.STAND_DOWN_CONFIRM_SECONDS", 0.0)
        monkeypatch.setattr("agentjobs.dispatch.standdown.STAND_DOWN_POLL_SECONDS", 0.0)

        result = finish(world)

        assert result.outcome == DECLINED and result.reason == "stand_down_unconfirmed"
        assert result.merge_commit is None
        assert not merged_into(world["root"], world["branch"])
        assert [run.run_id for run in live_runs(world["home"])] == [handle.run_id]
        holder = read_task_lock_holder(world["home"], world["task_id"], project_id="demo")
        assert holder is not None and holder.run_id == handle.run_id
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.WORK)
        assert task.ball_prompt == APPROVAL_CLEARANCE, "the approval must still stand"

        # The durable half: when the session does end, the poller starts the finish --
        # and does not wake the session to merge by hand beside it.
        spawned: List[Dict[str, Any]] = []

        def spawn_finish(**kwargs: Any) -> str:
            spawned.append(kwargs)
            return "spawned"

        monkeypatch.setattr("agentjobs.dispatch.finish.spawn_finish", spawn_finish)
        monkeypatch.setattr(
            "agentjobs.dispatch.finish.finish_is_offered", lambda project_id, home=None: True
        )
        runs_before = sorted(path.name for path in (world["home"] / "runs").iterdir())
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        from agentjobs.dispatch.poller import poll_live_sessions

        poll_live_sessions(world["home"])

        assert [call["task_id"] for call in spawned] == [world["task_id"]]
        assert result_for(world, handle.run_id).data["outcome"] == "completed"
        assert sorted(path.name for path in (world["home"] / "runs").iterdir()) == runs_before


# ----- sc-3 and durable-3: a Stop is not a stand-down ----------------------------


def ledger_for(world: Dict[str, Any], fake_cli: Path) -> DispatchLedger:
    return DispatchLedger(
        world["home"],
        registry=ProjectRegistry(home=world["home"]),
        managers={"demo": world["manager"]},
        session_command=[sys.executable, str(fake_cli)],
    )


class TestAStopIsADecisionAndSaysWhose:
    def test_stopping_an_approved_run_withdraws_the_approval_and_names_who_did_it(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        fake_cli = make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        hand_off_for_review(world)
        approval = approve(world)

        stopped = ledger_for(world, fake_cli).cancel(
            handle.run_id, actor=APPROVER, source="api", requester=APPROVER
        )

        assert stopped.stopped, stopped.detail
        task = the_task(world)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        prompt = task.ball_prompt or ""
        assert f"{APPROVER} stopped run {handle.run_id} (api)" in prompt
        assert "withdrew that approval" in prompt and "nothing was merged" in prompt
        assert "nobody was told" not in prompt
        config = world["project"].load_config()
        assert standing_approval_for(world["home"], task, config, project_id="demo") is None
        row = inbox_row(world, approval)
        assert row.status == "superseded"
        disposition = journal(world["home"]).signal_disposition(
            task_feed_source("demo"), row.source_event_id
        )
        assert disposition is not None and disposition["by"] == f"stop:{handle.run_id}"
        assert "stop_requested" in events_of(world, handle.run_id)

    def test_a_stop_that_cannot_be_confirmed_leaves_the_run_stopping_and_holding_the_task(
        self, world: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """from-264-stop."""
        fake_cli = make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        monkeypatch.setattr(
            DispatchLedger,
            "_stop_session",
            lambda self, record: StopResult(record.run_id, False, "`stop b55b35ad` exited 1"),
        )

        stopped = ledger_for(world, fake_cli).cancel(
            handle.run_id, actor=APPROVER, source="cli", requester=APPROVER
        )

        assert not stopped.stopped
        assert [run.run_id for run in live_runs(world["home"])] == [handle.run_id]
        holder = read_task_lock_holder(world["home"], world["task_id"], project_id="demo")
        assert holder is not None and holder.run_id == handle.run_id
        assert not [
            entry for entry in the_task(world).log if entry.type is LogEntryType.DISPATCH_RESULT
        ]
        attempt = journal(world["home"]).attempt(handle.run_id)
        assert attempt is not None and attempt.cancel_requested
        assert attempt.cancel is not None
        assert (attempt.cancel["requester"], attempt.cancel["source"]) == (APPROVER, "cli")
        meta = yaml.safe_load(
            (world["home"] / "runs" / handle.run_id / "meta.yaml").read_text(encoding="utf-8")
        )
        assert meta["stop_unconfirmed"]

        # Quiescence observed: the session is gone, and only now is the run cancelled.
        set_ledger(fake_cli, [])
        from agentjobs.dispatch.poller import poll_live_sessions

        poll_live_sessions(world["home"])

        assert live_runs(world["home"]) == []
        assert result_for(world, handle.run_id).data["outcome"] == "cancelled"
        task = the_task(world)
        assert task.ball is Ball.HUMAN
        assert f"{APPROVER} stopped run {handle.run_id} (cli)" in (task.ball_prompt or "")

    def test_a_stop_refuses_the_continuation_and_a_stand_down_does_not(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """task-375's refusal reads ``stop_requested``; a transfer must never trip it."""
        from agentjobs.dispatch.envelope import continuation_history
        from agentjobs.dispatch.journal import request_stand_down
        from agentjobs.dispatch.ledger import find_run

        make_session_dispatchable(world, tmp_path)
        handle = dispatch_session(world)
        store = journal(world["home"])

        request_stand_down(
            world["home"],
            find_run(world["home"], handle.run_id),
            requester=APPROVER,
            source="internal_transfer",
            reason="test",
            transfer_to="finish",
        )
        history = continuation_history(store, "demo", world["task_id"])
        assert history is not None and history.stop is None

        store.request_cancel(handle.run_id, requester=APPROVER, source="api")
        history = continuation_history(store, "demo", world["task_id"])
        assert history is not None and history.stop is not None


# ----- durable-4 and durable-5: the receipt ---------------------------------------


class TestTheApprovalReceipt:
    def test_it_names_the_reviewed_head_and_keeps_the_note_verbatim(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        make_session_dispatchable(world, tmp_path)
        approval = approve(world, note="Fold the naming nit in before you merge.")

        entry = the_task(world).log[-1]
        assert entry.id == approval
        receipt = entry.data["approval"]
        assert receipt["approver"] == APPROVER
        assert receipt["note"] == "Fold the naming nit in before you merge."
        assert receipt["reviewed"] == [
            {"name": world["branch"], "head": head(world["root"], world["branch"])}
        ]
        # Survives the process: a fresh store on the same file still owes it.
        reopened = ExecutionStore(world["home"] / "execution.db")
        row = reopened.signal(task_feed_source("demo"), inbox_row(world, approval).source_event_id)
        assert row is not None and row.status == "pending"
        assert row.payload["data"]["approval"]["note"] == "Fold the naming nit in before you merge."

    def test_a_later_request_for_changes_supersedes_it_without_erasing_it(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        make_session_dispatchable(world, tmp_path)
        approval = approve(world)
        revised = world["manager"].handoff(
            world["task_id"],
            actor=APPROVER,
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="On reflection, rename it first.",
        )

        supersede_earlier_approvals(world["home"], "demo", revised, by=revised.log[-1])

        config = world["project"].load_config()
        assert standing_approval_for(world["home"], revised, config, project_id="demo") is None
        row = inbox_row(world, approval)
        assert row.status == "superseded"
        assert any(entry.id == approval for entry in the_task(world).log)

    def test_the_synchronous_accept_and_the_feed_import_land_on_one_row(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        """Duplicate source delivery: two importers, one message."""
        make_session_dispatchable(world, tmp_path)
        approval = approve(world)
        store = journal(world["home"])

        import_source_events(store, "demo", task_feed(world["manager"]))
        import_source_events(store, "demo", task_feed(world["manager"]))

        wanted = inbox_row(world, approval).source_event_id
        assert [row.source_event_id for row in store.inbox(project_id="demo")].count(wanted) == 1


# ----- durable-1: no newest-only loss ---------------------------------------------


class TestEveryMessageIsDelivered:
    def test_a_wake_carries_every_message_since_the_session_last_ran(
        self, world: Dict[str, Any], tmp_path: Path
    ) -> None:
        make_session_dispatchable(world, tmp_path)
        dispatched = the_task(world).log[-1].id
        for feedback in ("Rename the helper.", "Also drop the debug print."):
            world["manager"].handoff(
                world["task_id"],
                actor=APPROVER,
                ball=Ball.AGENT,
                ball_reason=BallReason.REVISE,
                ball_prompt=feedback,
            )
        task = the_task(world)
        owed = human_handoffs_since(task, DISPATCHABLE_CONFIG, after_entry=dispatched)
        assert [entry.body for entry in owed] == [
            "Rename the helper.",
            "Also drop the debug print.",
        ]

        prompt = build_wake_prompt(
            agent="claude",
            task_id=task.id,
            ball_prompt=task.ball_prompt or "",
            api_base="http://127.0.0.1:1",
            run_id="run_new",
            previous_run_id="run_old",
            earlier=[entry.body or "" for entry in owed[:-1]],
        )
        assert "Rename the helper." in prompt and "Also drop the debug print." in prompt

    def test_dispositions_are_explicit_final_and_replayed(self, tmp_path: Path) -> None:
        store = ExecutionStore(tmp_path / "execution.db")
        execution = store.accept_execution("p", "task-1", envelope={}, workflow_version=1)
        source = task_feed_source("p")
        first, second = (
            SourceEvent(
                0, "p", "task-1", entry_id, f"2026-09-13T00:00:0{entry_id}Z", "handoff", "a"
            )
            for entry_id in (1, 2)
        )
        store.accept_signal(source, first)
        store.accept_signal(source, second)

        store.dispose_signal(
            source, first.source_event_id, status="consumed", disposition={"by": "r"}
        )
        again = store.dispose_signal(
            source, first.source_event_id, status="superseded", disposition={"by": "later"}
        )

        assert again is not None and again.status == "consumed", "a disposition is final"
        assert store.signal(source, second.source_event_id).status == "pending"  # type: ignore[union-attr]
        state = replay(
            execution.execution_id,
            [
                Event(event.sequence, event.kind, event.payload, event.source_id)
                for event in store.events(execution.execution_id)
            ],
        )
        assert state.pending_signals == (second.source_event_id,)


# ----- durable-2: an ambiguous send is not repeated --------------------------------


class TestAnAmbiguousDeliveryIsNotRepeated:
    def _pending_feedback(self, served: Any, tmp_path: Path) -> Any:
        from test_dispatch_handback import manager_for, seed_task, write_dispatch_config

        client, root, home = served
        write_dispatch_config(home, tmp_path, auto=True)
        task_id = seed_task(root)
        manager = manager_for(root)
        task = manager.handoff(
            task_id,
            actor=APPROVER,
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Rename the thing.",
        )
        journal(home).accept_execution("sandbox", task_id, envelope={}, workflow_version=1)
        return root, home, manager, task

    def _deliver(self, home: Path, manager: Any, task: Any) -> Any:
        from agentjobs.dispatch.handback import deliver_handback
        from test_dispatch_handback import CONFIG

        return deliver_handback(
            manager=manager,
            project=ProjectRegistry(home=home).get("sandbox"),
            project_config=CONFIG,
            task=task,
            home=home,
        )

    def _crashed_mid_send(self, home: Path, task: Any) -> str:
        """An intent with no result: the process died between the send and its receipt."""
        from agentjobs.dispatch.handback import _DeliveryReceipt

        receipt = _DeliveryReceipt.open(home, "sandbox", task, task.log[-1])
        assert receipt is not None
        receipt.intend()
        return receipt.activity_id

    def test_an_unreconcilable_intent_is_effect_unknown_and_nothing_is_sent(
        self, served: Any, tmp_path: Path
    ) -> None:
        from agentjobs.dispatch.handback import record_handback
        from test_dispatch_handback import runs_in

        _, home, manager, task = self._pending_feedback(served, tmp_path)
        activity_id = self._crashed_mid_send(home, task)

        outcome = self._deliver(home, manager, task)
        record_handback(manager, task, outcome)

        assert outcome.reason == "delivery_uncertain"
        assert runs_in(home) == [], "a second work instruction was sent"
        activity = next(a for a in journal(home).activities() if a.activity_id == activity_id)
        assert (activity.state, activity.error_class) == ("unknown", "effect_unknown")
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN

    def test_a_delivery_the_task_log_proves_is_reconciled_as_applied(
        self, served: Any, tmp_path: Path
    ) -> None:
        from agentjobs.models_v2 import DispatchMode, DispatchPosture, DispatchTrigger
        from test_dispatch_handback import runs_in

        _, home, manager, task = self._pending_feedback(served, tmp_path)
        activity_id = self._crashed_mid_send(home, task)
        manager.record_dispatch(
            task.id,
            actor=APPROVER,
            run_id="run_delivered",
            agent="fake",
            runner="fake",
            mode=DispatchMode.SESSION,
            posture=DispatchPosture.SUPERVISED,
            trigger=DispatchTrigger.AUTO,
            caused_by=task.log[-1].id,
            argv=["python"],
            cwd=".",
            git_head="abc1234",
        )

        outcome = self._deliver(home, manager, manager.get_task(task.id))

        assert outcome.reason == "already_delivered"
        assert runs_in(home) == []
        activity = next(a for a in journal(home).activities() if a.activity_id == activity_id)
        assert activity.state == "applied"


# ----- the reducer ------------------------------------------------------------------


class TestTheReducerKeepsTransferAndStopApart:
    def test_a_stand_down_proposes_a_transfer_and_never_a_stop(self) -> None:
        state = replay(
            "exe",
            [
                Event(1, "accepted", {"envelope": {}}),
                Event(2, "admitted", {"run_id": "run_1"}),
                Event(3, "launched", {"session_id": "s"}),
                Event(4, "stand_down_requested", {"transfer_to": "finish"}),
            ],
        )
        assert state.state == S_STANDING_DOWN
        assert [intent.kind for intent in next_intents(state)] == ["stand_down"]

    def test_a_stand_down_after_a_stop_does_not_undo_the_stop(self) -> None:
        state = replay(
            "exe",
            [
                Event(1, "accepted", {"envelope": {}}),
                Event(2, "admitted", {"run_id": "run_1"}),
                Event(3, "stop_requested", {"generation": 1, "requester": APPROVER}),
                Event(4, "stand_down_requested", {"transfer_to": "finish"}),
            ],
        )
        assert state.state == S_STOPPING
        assert [intent.kind for intent in next_intents(state)] == ["stop"]
