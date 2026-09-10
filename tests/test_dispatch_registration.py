"""Adopting a session AgentJobs did not start.

The defect these are about is an absence, so most of them assert on one: a refusal that
leaves **no** run directory, **no** log entry and **no** lock is the whole safety
argument, because a half-written registration is a run record naming a session nothing
can follow -- which looks covered and is not.

The last test is the one the task turns on. It registers a session the way a hand-spawned
agent would, then calls the *real* poller, and asserts the poller settled it onto the
task record. Nothing in the poller changed for that to work; the run record is the only
thing that was ever missing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from agentjobs.dispatch.ledger import list_runs, locks_root, read_run
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.registration import (
    DispatchedElsewhereError,
    RegistrationRunExistsError,
    RegistrationTaskClosedError,
    SessionUnknownError,
    SessionUnnamedError,
    UnknownActorError,
    register_session,
    self_session_id,
    session_kind,
)
from agentjobs.dispatch.runner import SessionPhase
from agentjobs.models_v2 import Lifecycle, LogEntryType, Outcome
from agentjobs.projects import ProjectRegistry

from test_dispatch_poller import _set_ledger, machine  # noqa: F401 - fixture import

SESSION = "b55b35ad"
FULL_SESSION = "b55b35ad-0000-4000-8000-000000000001"


def _row(session_id: str = SESSION, *, kind: str = "background", **extra: object) -> dict:
    """One ledger row shaped the way Claude Code 2.1.247 writes them."""
    row: dict = {
        "id": session_id,
        "sessionId": FULL_SESSION,
        "cwd": "C:/anywhere",
        "kind": kind,
        "state": "working",
        "status": "busy",
    }
    row.update(extra)
    if kind == "interactive":
        row.pop("id", None)
        row["pid"] = 4242
    return row


@pytest.fixture
def bench(machine):  # noqa: F811 - the poller's machine fixture, reused verbatim
    """The poller's machine, plus an open task and the arguments registration takes."""
    home, root, manager, fake_cli = machine
    created = manager.create_task(
        title="Hand-spawned",
        category="infrastructure",
        summary="A task somebody started a session on without dispatching it.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(created.id, agent="claude")
    project = ProjectRegistry(home=home).get("sandbox")
    _set_ledger(fake_cli, [_row()])
    return {
        "home": home,
        "root": root,
        "manager": manager,
        "fake_cli": fake_cli,
        "project": project,
        "task_id": created.id,
    }


def _register(bench, **overrides):
    kwargs = {
        "manager": bench["manager"],
        "project": bench["project"],
        "project_config": bench["project"].load_config(),
        "task_id": bench["task_id"],
        "session_id": SESSION,
        "home": bench["home"],
        "env": {},
    }
    kwargs.update(overrides)
    return register_session(**kwargs)


def _wrote_nothing(bench) -> None:
    """No run, no lock, and nothing on the record. What every refusal must leave."""
    assert list_runs(bench["home"]) == []
    locks = locks_root(bench["home"])
    assert not locks.is_dir() or list(locks.iterdir()) == []
    task = bench["manager"].get_task(bench["task_id"])
    assert not [entry for entry in task.log if (entry.data or {}).get("registration")]


# ----- the two driver-shaped questions ----------------------------------------


class TestSelfSessionId:
    def test_it_reads_the_short_id_out_of_the_full_session_uuid(self) -> None:
        from agentjobs.dispatch.config import RunnerDriver

        found = self_session_id(RunnerDriver.CLAUDE, {"CLAUDE_CODE_SESSION_ID": FULL_SESSION})
        assert found == SESSION

    def test_it_falls_back_to_the_job_directory_on_either_separator(self) -> None:
        from agentjobs.dispatch.config import RunnerDriver

        for job_dir in (
            "C:\\Users\\someone\\.claude\\jobs\\b55b35ad",
            "/home/someone/.claude/jobs/b55b35ad/",
        ):
            assert self_session_id(RunnerDriver.CLAUDE, {"CLAUDE_JOB_DIR": job_dir}) == SESSION

    def test_a_driver_that_publishes_nothing_answers_none_rather_than_guessing(self) -> None:
        from agentjobs.dispatch.config import RunnerDriver

        assert self_session_id(RunnerDriver.CODEX, {"CLAUDE_CODE_SESSION_ID": FULL_SESSION}) is None
        assert self_session_id(RunnerDriver.CLAUDE, {}) is None
        assert (
            self_session_id(RunnerDriver.CLAUDE, {"CLAUDE_CODE_SESSION_ID": "not-a-hex-id"}) is None
        )


class TestSessionKind:
    def test_it_reads_the_kind_the_ledger_states(self) -> None:
        assert session_kind(_row()) == "background"
        assert session_kind(_row(kind="interactive")) == "interactive"

    def test_without_a_kind_the_short_id_is_what_distinguishes_them(self) -> None:
        assert session_kind({"id": SESSION, "sessionId": FULL_SESSION}) == "background"
        assert session_kind({"pid": 42, "sessionId": FULL_SESSION}) == "interactive"


# ----- a dispatched run is already covered ------------------------------------


class TestAlreadyDispatched:
    def test_a_dispatched_session_is_told_it_is_known_and_nothing_is_written(self, bench) -> None:
        """The property that lets ALLAGENTS.md say "register" with no exception."""
        from agentjobs.dispatch.runner import RunDirectory

        RunDirectory.create(
            bench["home"],
            "run_already",
            {
                "run_id": "run_already",
                "task_id": bench["task_id"],
                "project_id": "sandbox",
                "mode": "session",
                "status": "running",
            },
        )
        result = _register(bench, env={"AGENTJOBS_RUN_ID": "run_already"})

        assert result.already_known is True
        assert result.run_id == "run_already"
        assert "Nothing to do" in result.detail
        assert [record.run_id for record in list_runs(bench["home"])] == ["run_already"]

    def test_a_dispatched_run_registering_against_another_task_is_refused(self, bench) -> None:
        from agentjobs.dispatch.runner import RunDirectory

        RunDirectory.create(
            bench["home"],
            "run_elsewhere",
            {
                "run_id": "run_elsewhere",
                "task_id": "task-999",
                "project_id": "sandbox",
                "mode": "session",
                "status": "running",
            },
        )
        with pytest.raises(DispatchedElsewhereError) as caught:
            _register(bench, env={"AGENTJOBS_RUN_ID": "run_elsewhere"})
        assert "task-999" in str(caught.value)

    def test_a_run_id_naming_a_finished_run_registers_afresh(self, bench) -> None:
        """A concluded run is not cover, so the session is adopted rather than waved on."""
        from agentjobs.dispatch.runner import RunDirectory

        RunDirectory.create(
            bench["home"],
            "run_over",
            {
                "run_id": "run_over",
                "task_id": bench["task_id"],
                "project_id": "sandbox",
                "mode": "session",
                "status": "finished",
            },
        )
        result = _register(bench, env={"AGENTJOBS_RUN_ID": "run_over"})
        assert result.already_known is False
        assert result.run_id != "run_over"


# ----- refusals, each of which must write nothing -----------------------------


class TestRefusals:
    def test_a_session_that_cannot_name_itself_is_told_what_to_pass(self, bench) -> None:
        with pytest.raises(SessionUnnamedError) as caught:
            _register(bench, session_id=None)
        assert "--session" in str(caught.value)
        _wrote_nothing(bench)

    def test_a_session_the_ledger_does_not_hold_is_refused(self, bench) -> None:
        with pytest.raises(SessionUnknownError) as caught:
            _register(bench, session_id="deadbeef")
        message = str(caught.value)
        assert "deadbeef" in message
        assert SESSION in message  # it names what *is* live, so the answer is actionable
        _wrote_nothing(bench)

    def test_a_session_that_is_over_is_refused_differently_from_one_that_never_was(
        self, bench
    ) -> None:
        """Wrong id and already dead are different mistakes with different fixes."""
        _set_ledger(bench["fake_cli"], [dict(_row(), state="stopped", status="stopped")])
        with pytest.raises(SessionUnknownError) as caught:
            _register(bench)
        assert "not live" in str(caught.value)
        _wrote_nothing(bench)

    def test_an_interactive_session_is_adopted_as_an_interactive_run(self, bench) -> None:
        """An attended session gets a record the poller cannot act on (task-354).

        This was a refusal until then, and the reason was sound at the time: a session
        run's record buys the poller's protections, and one of them ``stop``s a session
        that looks finished -- which, applied to a session somebody is typing into, ends
        it mid-sentence. What changed is that ``mode: interactive`` is a record those
        protections skip by construction, so the visibility can be had without them. The
        cost of refusing was measured on 2026-09-06: a task being worked in a chat window
        showed on the dashboard as *Runs 0*, with a Dispatch button offering to start a
        second agent on it.
        """
        _set_ledger(bench["fake_cli"], [_row(kind="interactive")])

        result = _register(bench, session_id=FULL_SESSION)

        assert not result.already_known
        record = read_run(Path(result.directory))
        assert record.is_interactive
        assert not record.takes_slot, "an attended session holds its task, not a slot"
        assert record.session_id == FULL_SESSION
        assert record.origin == "registered"

    def test_a_closed_task_is_refused(self, bench) -> None:
        bench["manager"].close_task(
            bench["task_id"], actor="claude", outcome=Outcome.COMPLETED, body="done"
        )
        with pytest.raises(RegistrationTaskClosedError):
            _register(bench)
        _wrote_nothing(bench)

    def test_a_task_that_already_has_a_live_run_is_refused(self, bench) -> None:
        from agentjobs.dispatch.runner import RunDirectory

        RunDirectory.create(
            bench["home"],
            "run_live",
            {
                "run_id": "run_live",
                "task_id": bench["task_id"],
                "project_id": "sandbox",
                "mode": "session",
                "status": "running",
            },
        )
        with pytest.raises(RegistrationRunExistsError) as caught:
            _register(bench)
        assert "run_live" in str(caught.value)
        assert [record.run_id for record in list_runs(bench["home"])] == ["run_live"]

    def test_an_actor_outside_the_projects_vocabulary_is_refused(self, bench) -> None:
        config_path = bench["root"] / ".agentjobs" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["actors"] = [{"id": "jeff", "kind": "human"}]
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        with pytest.raises(UnknownActorError):
            _register(bench, project_config=bench["project"].load_config(), actor="nobody")
        _wrote_nothing(bench)


# ----- the adoption itself ----------------------------------------------------


class TestAdoption:
    def test_it_writes_the_run_record_the_poller_needs(self, bench) -> None:
        result = _register(bench)

        record = read_run(Path(result.directory))
        assert record.task_id == bench["task_id"]
        assert record.project_id == "sandbox"
        assert record.mode == "session"
        assert record.status == "running"
        assert record.session_id == SESSION
        assert record.is_live
        # `poller.handle_from_record` skips a run without these two, silently. A registration
        # that produced such a run would be adopted by nothing.
        meta = yaml.safe_load((record.path / "meta.yaml").read_text(encoding="utf-8"))
        assert isinstance(meta["dispatch_entry_id"], int)
        assert meta["session_id"] == SESSION
        assert meta["origin"] == "registered"
        # No posture is claimed: AgentJobs did not choose this session's envelope.
        assert "posture" not in meta

    def test_it_takes_the_task_run_lock_so_a_dispatch_cannot_start_a_second_agent(
        self, bench
    ) -> None:
        _register(bench)
        lock = locks_root(bench["home"]) / f"{bench['task_id']}.lock"
        assert lock.is_file()
        assert "run_" in lock.read_text(encoding="utf-8")

    def test_the_task_record_says_a_session_was_adopted_and_what_that_buys(self, bench) -> None:
        """sc-2: the outcome is on the record, not only in a CLI's stdout."""
        result = _register(bench)
        task = bench["manager"].get_task(bench["task_id"])
        entry = task.log[-1]

        assert entry.id == result.entry_id
        assert entry.type is LogEntryType.NOTE
        assert SESSION in (entry.body or "")
        assert result.run_id in (entry.body or "")
        assert "AgentJobs did not start this session" in (entry.body or "")
        assert (entry.data or {})["registration"]["session_id"] == SESSION

    def test_it_is_not_recorded_as_a_dispatch(self, bench) -> None:
        """Nobody authorised a run and none was started; the D4 chain is not forged."""
        _register(bench)
        task = bench["manager"].get_task(bench["task_id"])
        assert not [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]

    def test_the_run_starts_when_the_session_did_not_when_it_was_adopted(self, bench) -> None:
        """The stale window and the recorded duration both measure from this."""
        began = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
        _set_ledger(bench["fake_cli"], [_row(startedAt=int(began.timestamp() * 1000))])
        result = _register(bench)
        assert read_run(Path(result.directory)).started_at == began


# ----- what it was all for ----------------------------------------------------


class TestThePollerFollowsARegisteredSession:
    def test_a_registered_session_that_disappears_is_concluded_on_the_record(self, bench) -> None:
        """The task-217 shape, end to end, through the unmodified poller.

        Before this, a hand-spawned session was never in `live_runs`, so nothing polled
        it and the task read `agent`/`work` for ever. Registering it is the only change:
        the poller, `handle_from_record`, `poll_session` and `_finish_session` are untouched.
        """
        _register(bench)

        # The session goes away, which is what the ledger says when a hand-spawned agent
        # exits and nobody wrote down that it had.
        _set_ledger(bench["fake_cli"], [])
        results = poll_live_sessions(bench["home"])

        assert [result.phase for result in results] == [SessionPhase.GONE]
        task = bench["manager"].get_task(bench["task_id"])
        result = [entry for entry in task.log if entry.type.value == "dispatch_result"]
        assert len(result) == 1
        assert (result[0].data or {})["outcome"] == "interrupted"
        # The ball is deliberately not asserted to have moved. `poll_session`'s GONE path
        # writes the result and hands off to nobody, for dispatched and registered runs
        # alike; whether a vanished session should also move the ball is a question about
        # that path, not about this one, and it is filed rather than changed here.

    def test_before_registering_the_same_session_is_invisible_to_the_poller(self, bench) -> None:
        """The defect itself, asserted, so the test above is measuring something."""
        _set_ledger(bench["fake_cli"], [])
        assert poll_live_sessions(bench["home"]) == []
        task = bench["manager"].get_task(bench["task_id"])
        assert task.ball.value == "agent"
        assert [entry for entry in task.log if entry.type.value == "dispatch_result"] == []

    def test_a_registered_session_that_goes_quiet_is_reported_as_stalled(self, bench) -> None:
        """The protection task-296 shipped, now reaching a session it never could."""
        _register(bench)

        # Two polls with an unchanged transcript, and a stall window of nothing, is what
        # `_check_running_stall` needs to see silence rather than a first observation.
        first = poll_live_sessions(bench["home"])
        assert [result.phase for result in first] == [SessionPhase.RUNNING]

        record = list_runs(bench["home"])[0]
        meta = yaml.safe_load((record.path / "meta.yaml").read_text(encoding="utf-8"))
        meta["output_changed_at"] = "2026-08-27T00:00:00+00:00"
        (record.path / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

        poll_live_sessions(bench["home"])
        task = bench["manager"].get_task(bench["task_id"])
        assert task.ball.value == "human"
        assert "no output" in (task.ball_prompt or "")
        # Not "Dispatched session": nobody dispatched this one, and the person reading
        # this prompt is deciding what to do about a session somebody else spawned.
        assert (task.ball_prompt or "").startswith("Registered session")
        assert read_run(record.path).status == "stalled"


def test_the_fake_ledger_is_shaped_like_the_real_one() -> None:
    """A guard on the fixture: these tests are only evidence if the rows are real ones.

    The shape was read off `claude agents --json` on 2.1.247, 2026-08-27. `status` is
    present only on a busy session, which is why nothing here requires it.
    """
    assert json.loads(json.dumps(_row())).keys() >= {"id", "sessionId", "cwd", "kind"}


# ----- through the command a session actually types ---------------------------


class TestTheCommand:
    """`agentjobs run register` end to end, because that is the surface AGENTS.md names.

    Invoked through Typer rather than by calling `register_session`, so the option names
    an agent is told to type are part of what these assert.
    """

    def _cli(self, bench, monkeypatch, *args: str):
        from typer.testing import CliRunner

        from agentjobs import cli as cli_module
        from agentjobs.cli import app

        monkeypatch.setenv("AGENTJOBS_HOME", str(bench["home"]))
        # The command resolves its manager the ordinary way, which outside the server is
        # a service client -- so exercising the Typer surface would otherwise need a
        # server running. What these two assert is the option names an agent is told to
        # type and what the command prints, so the manager is supplied.
        monkeypatch.setattr(cli_module, "task_manager_for", lambda project: bench["manager"])
        # A test process inherits its own dispatch environment. Left set, every case
        # below would take the already-known path against a run that is not in this
        # temporary home.
        monkeypatch.delenv("AGENTJOBS_RUN_ID", raising=False)
        return CliRunner().invoke(app, ["run", "register", *args])

    def test_it_registers_and_says_where_the_run_landed(self, bench, monkeypatch) -> None:
        result = self._cli(
            bench,
            monkeypatch,
            "--task",
            bench["task_id"],
            "--project",
            "sandbox",
            "--session",
            SESSION,
        )

        assert result.exit_code == 0, result.output
        assert "adopted as run" in result.output
        assert "Run directory" in result.output
        assert len(list_runs(bench["home"])) == 1

    def test_a_refusal_names_its_gate_and_exits_non_zero(self, bench, monkeypatch) -> None:
        result = self._cli(
            bench,
            monkeypatch,
            "--task",
            bench["task_id"],
            "--project",
            "sandbox",
            "--session",
            "deadbeef",
        )

        assert result.exit_code == 1
        assert "Refused (session_unknown)" in result.output
        assert list_runs(bench["home"]) == []
