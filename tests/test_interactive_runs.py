"""Runs for sessions a person is sitting in (task-354).

The defect was an absence: task-352 was claimed and being worked from a chat session,
and the dashboard said *Runs 0* with a Dispatch button on it. So the assertions here are
mostly about a record existing, and about the three things it must **not** drag in with
it -- a slot, a poller that stops the session, and a second run on the same task.

The fake CLI is the one ``test_dispatch_runner.py`` defines and the poller's tests reuse:
session mode is "a runner whose executable answers ``agents --json``", and a fake that
answers it exercises the real path rather than a mock of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

from agentjobs.dispatch.guards import LiveRunExistsError, live_runs
from agentjobs.dispatch.interactive import (
    ORIGIN_CLAIMED,
    settle_for_task,
    start_interactive_run,
    sweep_interactive_runs,
)
from agentjobs.dispatch.ledger import (
    RunRecord,
    DispatchLedger,
    HEALTH_IDLE,
    HEALTH_WORKING,
    list_runs,
    read_run,
    run_health,
    slot_runs,
)
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.models_v2 import Ball, BallReason, DispatchMode, Lifecycle, Outcome
from agentjobs.projects import ProjectRegistry
from agentjobs.session_identity import SESSION_ID_ENV, SessionIdentity
from test_dispatch_poller import _set_ledger, machine  # noqa: F401 - fixture import

SESSION = "0feedf93-6af3-40bf-832a-81f722fdb841"
"""A full session uuid, the shape Claude Code publishes to its children."""


@pytest.fixture
def bench(machine):  # noqa: F811 - the poller's machine fixture, reused verbatim
    """A registered project with one claimed task, ready to be given a session."""
    home, root, manager, fake_cli = machine
    # Actors, so a dispatch can be human-clocked: the refusal this suite wants to see is
    # `live_run_exists`, and without a configured human the guard chain refuses earlier
    # for an unrelated reason.
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Sandbox",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "claude", "kind": "agent"},
                    # The fixture's runner is named `fake` and acts as itself.
                    {"name": "fake", "kind": "agent"},
                ],
                "default_user": "Jeff Posey",
            }
        ),
        encoding="utf-8",
    )
    created = manager.create_task(
        title="Worked in a chat window",
        category="infrastructure",
        summary="A task somebody is working from a session AgentJobs did not start.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(created.id, agent="claude")
    project = ProjectRegistry(home=home).get("sandbox")
    return {
        "home": home,
        "root": root,
        "manager": manager,
        "fake_cli": fake_cli,
        "project": project,
        "task_id": created.id,
    }


def _try_start(bench, *, session_id: str = SESSION, cwd: str = "") -> Optional[RunRecord]:
    """Claim-time record for this bench's task, or None the way the real caller sees it."""
    task = bench["manager"].get_task(bench["task_id"])
    return start_interactive_run(
        home=bench["home"],
        project=bench["project"],
        task=task,
        identity=SessionIdentity(session_id=session_id, cwd=cwd or str(bench["root"])),
        actor="claude",
        origin=ORIGIN_CLAIMED,
    )


def _start(bench, *, session_id: str = SESSION, cwd: str = "") -> RunRecord:
    """The same, for the tests that go on to read the record it wrote."""
    record = _try_start(bench, session_id=session_id, cwd=cwd)
    assert record is not None, "the fixture's task should have accepted a session"
    return record


def _ledger_rows(bench) -> List[dict]:
    """What the fake CLI would answer with now."""
    import json

    return list(json.loads((bench["fake_cli"].parent / "ledger.json").read_text()))


def _rows(session_id: str = SESSION) -> List[dict]:
    """A ledger listing an interactive session, as Claude Code writes them."""
    return [
        {
            "pid": 4242,
            "sessionId": session_id,
            "cwd": "C:/anywhere",
            "kind": "interactive",
            "name": "agentjobs-00",
        }
    ]


class TestTheRecordExists:
    """The defect itself: work with no run record is work the GUI cannot see."""

    def test_a_session_that_claims_gets_a_run(self, bench) -> None:
        record = _start(bench)

        assert record is not None
        assert record.mode == DispatchMode.INTERACTIVE.value
        assert record.is_interactive
        assert record.session_id == SESSION
        assert record.status == "running"
        assert record.task_id == bench["task_id"]
        assert [run.run_id for run in live_runs(bench["home"])] == [record.run_id]

    def test_it_records_where_the_session_runs(self, bench) -> None:
        # The transcript store is keyed on the session's directory, and a session
        # working from a worktree is not in the project root. Without this the task
        # page looks for its log in the wrong place.
        record = _start(bench, cwd="C:/projects/worktrees/agentjobs-354")

        assert record is not None
        assert record.cwd == "C:/projects/worktrees/agentjobs-354"
        assert record.origin == ORIGIN_CLAIMED

    def test_no_identity_writes_no_run(self, bench) -> None:
        # A human at the keyboard, or any caller that named no session. The ordinary
        # case, and it must leave the ledger exactly as it was.
        task = bench["manager"].get_task(bench["task_id"])
        assert (
            start_interactive_run(
                home=bench["home"],
                project=bench["project"],
                task=task,
                identity=None,
                actor="Jeff Posey",
            )
            is None
        )
        assert list_runs(bench["home"]) == []

    def test_a_second_claim_does_not_write_a_second_run(self, bench) -> None:
        # A replayed claim, or a dispatched agent claiming on arrival. One live run per
        # task, always -- the same rule the dispatch guard enforces.
        first = _start(bench)
        assert _try_start(bench) is None
        assert [run.run_id for run in live_runs(bench["home"])] == [first.run_id]


class TestItHoldsNoSlot:
    """`occupied` is slots; an interactive session is running and holds none."""

    def test_it_is_live_but_not_a_slot(self, bench) -> None:
        _start(bench)

        assert len(live_runs(bench["home"])) == 1
        assert slot_runs(bench["home"]) == []

    def test_a_dispatch_is_not_refused_for_the_machine_being_full(self, bench) -> None:
        # The ceiling in the poller's fixture is the default. What matters is that an
        # interactive run is not what stands between a click and a free machine.
        from agentjobs.dispatch.guards import live_runs as guard_runs

        _start(bench)
        holding = [run for run in guard_runs(bench["home"]) if run.takes_slot]

        assert holding == []
        assert [run.is_interactive for run in guard_runs(bench["home"])] == [True]

    def test_but_it_does_hold_its_task(self, bench) -> None:
        # This is the Dispatch button fix: a task an interactive session is working is
        # refused, so a click cannot start a second agent on it. The refusal says which
        # kind of run holds it, because "already has a run" on a task the reader is
        # working in another window is otherwise baffling.
        from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
        from agentjobs.models_v2 import LogEntryType

        _start(bench)
        clicked = bench["manager"].add_log_entry(
            bench["task_id"],
            actor="Jeff Posey",
            type=LogEntryType.NOTE,
            body="Start an agent on this.",
        )
        with pytest.raises(LiveRunExistsError) as refusal:
            dispatch_task(
                manager=bench["manager"],
                project=bench["project"],
                project_config=bench["project"].load_config(),
                request=DispatchRequest(task_id=bench["task_id"], caused_by=clicked.log[-1].id),
                home=bench["home"],
            )

        assert "interactive session is working it" in str(refusal.value)


class TestThePollerLeavesItAlone:
    """The reason interactive sessions were refused a record until now."""

    def test_polling_never_stops_parks_or_settles_it(self, bench) -> None:
        record = _start(bench)
        # A ledger that would park a *dispatched* session: blocked on a prompt.
        _set_ledger(
            bench["fake_cli"],
            [{"pid": 4242, "sessionId": SESSION, "kind": "interactive", "state": "blocked"}],
        )
        before = bench["manager"].get_task(bench["task_id"])

        poll_live_sessions(bench["home"])

        after = bench["manager"].get_task(bench["task_id"])
        assert read_run(record.path).status == "running"
        assert after.ball is Ball.AGENT
        assert len(after.log) == len(before.log), "nothing may be written to the task"

    def test_cancelling_the_record_does_not_touch_the_session(self, bench) -> None:
        record = _start(bench)
        _set_ledger(bench["fake_cli"], _rows())
        ledger = DispatchLedger(bench["home"])

        result = ledger.cancel(record.run_id, actor="Jeff Posey")

        assert result.stopped
        assert "left running" in result.detail
        assert not read_run(record.path).is_live
        # The fake CLI's `stop` removes the row it was given. The row is still there,
        # which is the assertion that matters: nothing asked the session to end.
        assert _ledger_rows(bench) == _rows()


class TestItEndsWhenTheWorkDoes:
    def test_a_handoff_to_a_human_ends_it(self, bench) -> None:
        record = _start(bench)
        bench["manager"].handoff(
            bench["task_id"],
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please look at this.",
        )

        settle_for_task(
            bench["home"], bench["manager"].get_task(bench["task_id"]), bench["task_id"]
        )

        ended = read_run(record.path)
        assert not ended.is_live
        assert ended.outcome == "completed"

    def test_closing_the_task_ends_it(self, bench) -> None:
        record = _start(bench)
        bench["manager"].close_task(
            bench["task_id"], actor="claude", outcome=Outcome.COMPLETED, body="Done."
        )

        settle_for_task(
            bench["home"], bench["manager"].get_task(bench["task_id"]), bench["task_id"]
        )

        assert not read_run(record.path).is_live

    def test_the_sweep_ends_a_run_whose_session_is_gone(self, bench) -> None:
        record = _start(bench)
        _set_ledger(bench["fake_cli"], [])  # the window was closed

        results = sweep_interactive_runs(bench["home"])

        ended = read_run(record.path)
        assert not ended.is_live
        assert ended.outcome == "session_ended"
        assert [result.concluded for result in results] == [True]

    def test_the_sweep_keeps_a_run_whose_session_is_still_there(self, bench) -> None:
        record = _start(bench)
        _set_ledger(bench["fake_cli"], _rows())

        sweep_interactive_runs(bench["home"])

        assert read_run(record.path).is_live

    def test_an_unreadable_ledger_ends_nothing(self, bench) -> None:
        # Declaring live work dead on a failed lookup is the one mistake here that the
        # next tick cannot undo.
        record = _start(bench)
        (bench["fake_cli"].parent / "ledger.json").write_text("not json", encoding="utf-8")

        results = sweep_interactive_runs(bench["home"])

        assert read_run(record.path).is_live
        assert any("could not read" in result.detail for result in results)


class TestHealth:
    """Nothing writes a status for these, so the transcript's mtime is the evidence."""

    def test_a_session_writing_its_transcript_is_working(
        self, bench, tmp_path, monkeypatch
    ) -> None:
        record = _start(bench)
        _write_transcript(tmp_path, bench, monkeypatch, age_seconds=5)

        assert run_health(read_run(record.path)) == HEALTH_WORKING

    def test_a_session_that_has_gone_quiet_is_idle_rather_than_working(
        self, bench, tmp_path, monkeypatch
    ) -> None:
        record = _start(bench)
        _write_transcript(tmp_path, bench, monkeypatch, age_seconds=3600)

        assert run_health(read_run(record.path)) == HEALTH_IDLE

    def test_a_session_with_no_transcript_at_all_is_working(
        self, bench, tmp_path, monkeypatch
    ) -> None:
        # A driver that keeps no such file, or a session in its first second. Absence is
        # not evidence of idleness, and rendering it as such would be an invention.
        record = _start(bench)
        monkeypatch.setattr(
            "agentjobs.dispatch.transcript.claude_projects_dir", lambda home=None: tmp_path / "no"
        )

        assert run_health(read_run(record.path)) == HEALTH_WORKING


def _write_transcript(tmp_path: Path, bench, monkeypatch, *, age_seconds: int) -> Path:
    """A session transcript where `find_session_transcript` looks for one.

    The store is redirected into ``tmp_path``: this reads a real path off a real clock,
    and a test that wrote into the developer's own ``~/.claude`` would be leaving files
    in it.
    """
    import os

    from agentjobs.dispatch.transcript import project_slug

    root = tmp_path / "claude-projects"
    monkeypatch.setattr("agentjobs.dispatch.transcript.claude_projects_dir", lambda home=None: root)
    store = root / project_slug(Path(bench["root"]))
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{SESSION}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    when = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).timestamp()
    os.utime(path, (when, when))
    return path


class TestSessionIdentity:
    def test_it_reads_this_process_session_out_of_the_environment(self) -> None:
        found = SessionIdentity.from_environment({SESSION_ID_ENV: SESSION}, cwd=Path("C:/x"))

        assert found is not None
        assert found.session_id == SESSION
        assert found.cwd == "C:\\x" or found.cwd == "C:/x"

    def test_a_process_outside_a_session_names_none(self) -> None:
        assert SessionIdentity.from_environment({}) is None

    def test_a_dispatched_run_names_none(self) -> None:
        # It already has a run record. A second one would put two runs on one task.
        assert (
            SessionIdentity.from_environment(
                {SESSION_ID_ENV: SESSION, "AGENTJOBS_RUN_ID": "run_abc123"}
            )
            is None
        )
