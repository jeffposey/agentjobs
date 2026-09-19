"""The subscription as a start gate: what stops, what does not, and what resumes (task-463).

Every test here drives the **real** ``Controller.tick()`` over task-416's ``Machine``
harness, with production ``queue.start_due`` and ``pull.pull_due`` inside it and no gate
stubbed, for the reason ``test_dispatch_queue.py`` gives: the claim is about two clickless
starters behaving a particular way, and a test that mocked the starters would be asserting
the claim rather than checking it.

Two things are deliberately not faked.

* **The incident is a row in task-417's own table**, written the way its book writes one,
  including the ``profile_json`` a credential is read back out of. A fixture that stubbed
  ``open_incidents`` would pass while the two modules disagreed about what a credential is,
  which is the one mistake that would make this gate silently do nothing.
* **"Nothing was spent" is asserted against the records that hold the spend** -- the task
  logs, the ledger, and the arming row's ``started`` count -- rather than against a mock's
  call count.

The incident in ``resets_at`` below is the real one from this machine on 2026-09-18:
``inc_7ffcc0210a984e39``, ``usage_limit``, opened 21:58Z with a 23:40Z reset, which parked
a run for 1h44m and recovered itself. The shape is copied rather than invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from agentjobs.dispatch import pull as dispatch_pull
from agentjobs.dispatch import start_pause
from agentjobs.dispatch.auth_recovery import Profile, book_for
from agentjobs.dispatch.config import load_dispatch_config
from agentjobs.dispatch.guards import IF_FULL_QUEUE, DispatchRequest
from agentjobs.dispatch.budget import dispatches_since
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import live_runs
from agentjobs.dispatch.queue import dispatch_or_queue
from agentjobs.execution.store import BOUND_OPEN, BOUND_STARTS, PULL_ARMED
from agentjobs.projects import ProjectRegistry

from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


ARMER = "Jeff Posey"
RESET = datetime(2026, 9, 18, 23, 40, tzinfo=timezone.utc)


# ----- helpers ------------------------------------------------------------------


def runner_definition(box: Machine, name: str = "fake") -> Any:
    config = load_dispatch_config(box.home)
    assert config is not None
    return config.runners[name]


def claude_profile(box: Machine, runner: str = "fake") -> Profile:
    """The credential a start on this runner would spend, as the book would record it."""
    return start_pause.profile_for_runner(runner_definition(box, runner))


def codex_profile() -> Profile:
    """A credential belonging to a different CLI entirely."""
    return Profile(driver="codex", executable=(), model=None, claude_home="/nowhere", auth_env=())


def seed_incident(
    box: Machine,
    profile: Profile,
    *,
    incident_id: str = "inc_7ffcc0210a984e39",
    kind: str = "usage_limit",
    resets_at: Optional[datetime] = RESET,
) -> str:
    """One open incident, written exactly as ``IncidentBook.join`` writes one."""
    moment = datetime.now(timezone.utc).isoformat()
    with journal(box.home).transaction("test-incident") as connection:
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, "
            "state, opened_at, next_probe_at, resets_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?,?)",
            (
                incident_id,
                kind,
                profile.key,
                json.dumps(profile.as_json()),
                moment,
                moment,
                resets_at.isoformat() if resets_at else None,
                moment,
            ),
        )
    return incident_id


def close_incident(box: Machine, incident_id: str) -> None:
    """What task-417's probe does when the reset has passed. Nothing here decides it."""
    book_for(box.home).close(
        incident_id, state="closed", reason="probe succeeded", now=datetime.now(timezone.utc)
    )


def arm(box: Machine, **kwargs: Any) -> Any:
    project = ProjectRegistry(home=box.home).get("sandbox")
    kwargs.setdefault("bound_kind", BOUND_OPEN)
    return dispatch_pull.arm(box.home, project, project.load_config(), armed_by=ARMER, **kwargs)


def enqueue(box: Machine, task_id: str, *, runner: Optional[str] = None) -> Any:
    """A real queued dispatch, through the route's own function, onto a full machine."""
    project = ProjectRegistry(box.home).get("sandbox")
    return dispatch_or_queue(
        manager=box.manager,
        project=project,
        project_config=project.load_config(),
        request=DispatchRequest(
            task_id=task_id,
            caused_by=box.authorised_by,
            runner=runner,
            if_full=IF_FULL_QUEUE,
        ),
        home=box.home,
        api_base="http://127.0.0.1:9",
    )


def fill_the_machine(box: Machine) -> List[str]:
    """Start runs until there is no slot left, so a queued entry really has to wait."""
    started: List[str] = []
    for _ in range(3):
        task_id = box.task()
        box.dispatch(task_id)
        started.append(task_id)
    return started


def free_one_slot(box: Machine, task_id: str) -> None:
    """End the run holding this task's slot, in the place the ceiling reads."""
    for record in live_runs(box.home):
        if record.task_id == task_id:
            journal(box.home).conclude(
                record.run_id, outcome="completed", status="finished", concluded_by="test"
            )
            return
    raise AssertionError(f"{task_id} has no live run")


def arming_row(box: Machine, arming_id: str) -> Any:
    row = journal(box.home).pull_arming(arming_id)
    assert row is not None
    return row


def log_length(box: Machine, task_id: str) -> int:
    task = box.manager.get_task(task_id)
    assert task is not None
    return len(task.log)


def held_lines(lines: List[str]) -> List[str]:
    return [line for line in lines if "paused" in line]


# ----- what a credential is ------------------------------------------------------


class TestCredentialGranularity:
    """ac-3's decision, at the level it is actually made."""

    def test_the_model_is_not_part_of_it(self, machine: Machine) -> None:
        """A subscription's window is charged to the login, not to the model that hit it."""
        opus = Profile("claude", ("claude",), "claude-opus-5", "/home/.claude")
        sonnet = Profile("claude", ("claude",), "claude-sonnet-5", "/home/.claude")

        assert opus.key != sonnet.key  # they do not share a *probe*
        assert opus.credential_key == sonnet.credential_key  # they do share a *quota*

    def test_a_different_driver_is_a_different_credential(self) -> None:
        claude = Profile("claude", (), None, "/home/.claude")
        codex = Profile("codex", (), None, "/home/.claude")

        assert claude.credential_key != codex.credential_key

    def test_a_different_home_is_a_different_credential(self) -> None:
        one = Profile("claude", (), None, "/home/.claude")
        other = Profile("claude", (), None, "/home/.claude-work")

        assert one.credential_key != other.credential_key

    def test_an_auth_env_override_is_a_different_credential(self) -> None:
        subscription = Profile("claude", (), None, "/home/.claude")
        keyed = Profile("claude", (), None, "/home/.claude", ("ANTHROPIC_API_KEY",))

        assert subscription.credential_key != keyed.credential_key

    def test_a_runners_credential_matches_the_one_a_run_would_open(self, machine: Machine) -> None:
        """The gate and the book have to agree, or the gate silently never fires."""
        definition = runner_definition(machine)

        assert (
            start_pause.credential_for_runner(definition) == claude_profile(machine).credential_key
        )


# ----- ac-1: nothing starts, and nothing is spent --------------------------------


class TestTheQueuePauses:
    def test_a_queued_entry_does_not_start_and_keeps_its_place(self, machine: Machine) -> None:
        holding = fill_the_machine(machine)
        waiting = machine.task()
        entry = enqueue(machine, waiting)
        seed_incident(machine, claude_profile(machine))
        free_one_slot(machine, holding[0])
        before = log_length(machine, waiting)

        lines = machine.tick(3)

        assert waiting not in [row["name"].rsplit("/", 1)[-1] for row in machine.rows()]
        still = journal(machine.home).queued_dispatch(entry.queue_id)
        assert still is not None and still.waiting
        assert still.attempts == 0, "the entry was put through no gate, so it spent no attempt"
        assert log_length(machine, waiting) == before, "nothing was written to the task"
        assert held_lines(lines), "the tick said why it started nothing"

    def test_the_hourly_cap_is_not_charged(self, machine: Machine) -> None:
        """The cap counts starts. A start that never happened must not count as one."""
        holding = fill_the_machine(machine)
        waiting = machine.task()
        enqueue(machine, waiting)
        seed_incident(machine, claude_profile(machine))
        free_one_slot(machine, holding[0])
        an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        before = len(dispatches_since(machine.home, an_hour_ago))

        machine.tick(5)

        after = len(dispatches_since(machine.home, an_hour_ago))
        assert after == before, "no run was admitted, so the hourly cap counted nothing"

    def test_the_reason_is_said_once_however_many_entries_wait(self, machine: Machine) -> None:
        holding = fill_the_machine(machine)
        for _ in range(3):
            enqueue(machine, machine.task())
        seed_incident(machine, claude_profile(machine))
        free_one_slot(machine, holding[0])

        lines = machine.tick()

        assert len(held_lines(lines)) == 1


class TestThePullModePauses:
    def test_an_armed_project_starts_nothing(self, machine: Machine) -> None:
        task_id = machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        seed_incident(machine, claude_profile(machine))
        before = log_length(machine, task_id)

        lines = machine.tick(3)

        assert machine.rows() == []
        assert log_length(machine, task_id) == before
        assert held_lines(lines)

    def test_the_arming_keeps_its_bound_and_its_authority(self, machine: Machine) -> None:
        """The whole difference from the disarm this replaces (task-462's ``_incident_stall``)."""
        machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        seed_incident(machine, claude_profile(machine))

        machine.tick(3)

        row = arming_row(machine, armed.arming_id)
        assert row.state == PULL_ARMED, "a paused arming is still armed"
        assert row.started == 0
        assert row.armed_by == ARMER, "the authority it starts on is untouched"

    def test_a_pause_is_not_a_failure(self, machine: Machine) -> None:
        """Three held ticks must not trip ``FAILURE_RUN_LIMIT`` and retire the arming."""
        machine.task()
        armed = arm(machine)
        seed_incident(machine, claude_profile(machine))

        machine.tick(6)

        assert arming_row(machine, armed.arming_id).failures == 0


# ----- ac-2: the reset starts it again, with no second probe ---------------------


class TestResume:
    def test_the_queue_starts_on_the_first_tick_after_the_incident_closes(
        self, machine: Machine
    ) -> None:
        holding = fill_the_machine(machine)
        waiting = machine.task()
        enqueue(machine, waiting)
        incident = seed_incident(machine, claude_profile(machine))
        free_one_slot(machine, holding[0])
        machine.tick(2)
        assert waiting not in [row["name"].rsplit("/", 1)[-1] for row in machine.rows()]

        close_incident(machine, incident)
        machine.tick()

        assert waiting in [row["name"].rsplit("/", 1)[-1] for row in machine.rows()]

    def test_the_pull_mode_starts_on_the_first_tick_after_the_incident_closes(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        incident = seed_incident(machine, claude_profile(machine))
        machine.tick(2)
        assert machine.rows() == []

        close_incident(machine, incident)
        machine.tick()

        assert [row["name"].rsplit("/", 1)[-1] for row in machine.rows()] == [task_id]
        assert arming_row(machine, armed.arming_id).started == 1

    def test_nothing_here_probes(self, machine: Machine) -> None:
        """No second probe was added: the gate reads the book and never writes to it.

        Asserted against the probe table task-417 owns, because "we did not add a probe"
        is a claim about behaviour and not about a diff.
        """
        machine.task()
        arm(machine)
        seed_incident(machine, claude_profile(machine))

        machine.tick(5)

        rows = journal(machine.home).read("SELECT COUNT(*) AS n FROM auth_probe")
        assert rows[0]["n"] == 0

    def test_the_pause_survives_a_restart_with_no_state_to_recover(self, machine: Machine) -> None:
        """A fresh controller object -- nothing carried over -- still holds and still resumes."""
        task_id = machine.task()
        arm(machine)
        incident = seed_incident(machine, claude_profile(machine))

        machine.controller().tick()
        assert machine.rows() == []
        close_incident(machine, incident)
        machine.controller().tick()

        assert [row["name"].rsplit("/", 1)[-1] for row in machine.rows()] == [task_id]


# ----- ac-3: one credential's incident is not another's ---------------------------


class TestPerCredential:
    def test_a_codex_incident_does_not_pause_a_claude_runner(self, machine: Machine) -> None:
        task_id = machine.task()
        arm(machine)
        seed_incident(machine, codex_profile(), incident_id="inc_codex")

        machine.tick()

        assert [row["name"].rsplit("/", 1)[-1] for row in machine.rows()] == [task_id]

    def test_an_entry_on_an_unaffected_runner_starts_beside_a_held_one(
        self, machine: Machine
    ) -> None:
        """Two Claude runners, one of them on an API key: different credentials.

        The keyed runner is a different credential by exactly the rule ``profile_for``
        uses -- the *names* of the auth environment variables it would run under -- so an
        incident against the subscription leaves it starting.
        """
        import sys

        machine.configure(
            runners={
                "keyed": {
                    "mode": "session",
                    "actor": "claude",
                    "argv": [sys.executable, str(machine.fake_cli), "--bg", "{prompt}"],
                    "env": {"ANTHROPIC_API_KEY": "not-a-real-key"},
                }
            }
        )
        holding = fill_the_machine(machine)
        held = machine.task()
        enqueue(machine, held, runner="fake")
        starts = machine.task()
        enqueue(machine, starts, runner="keyed")
        seed_incident(machine, claude_profile(machine, "fake"))
        free_one_slot(machine, holding[0])

        machine.tick(2)

        started = [row["name"].rsplit("/", 1)[-1] for row in machine.rows()]
        assert starts in started
        assert held not in started


# ----- what the sentence promises -------------------------------------------------


class TestWhatItSays:
    def test_a_usage_limit_names_its_reset(self, machine: Machine) -> None:
        seed_incident(machine, claude_profile(machine))

        pause = start_pause.pause_for(machine.home, "sandbox")

        assert pause is not None
        assert pause.resets_at == RESET
        assert pause.resumes_by_itself
        assert "usage limit" in pause.sentence()
        assert pause.runner == "fake"

    @pytest.mark.parametrize("kind", ["auth", "spend_limit"])
    def test_a_kind_that_needs_a_person_promises_no_time(self, machine: Machine, kind: str) -> None:
        """Decided on task-463: every kind pauses, but only a usage limit gets a clock."""
        seed_incident(machine, claude_profile(machine), kind=kind, resets_at=None)

        pause = start_pause.pause_for(machine.home, "sandbox")

        assert pause is not None
        assert not pause.resumes_by_itself
        assert "until the incident clears" in pause.sentence()

    def test_a_usage_limit_with_no_reset_promises_no_time_either(self, machine: Machine) -> None:
        seed_incident(machine, claude_profile(machine), resets_at=None)

        pause = start_pause.pause_for(machine.home, "sandbox")

        assert pause is not None and not pause.resumes_by_itself

    def test_an_unreadable_book_is_not_evidence_of_an_incident(
        self, machine: Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(_: Any) -> Any:
            raise OSError("the store is gone")

        monkeypatch.setattr(start_pause, "book_for", explode)

        assert start_pause.open_pauses(machine.home) == {}


# ----- ac-4's other half: a manual click is unchanged -----------------------------


class TestAManualClickIsUnchanged:
    def test_dispatch_still_starts_during_an_incident(self, machine: Machine) -> None:
        """The person is there to read task-417's park. A tick is not.

        This is the asymmetry the gate is *for*, so it is asserted rather than assumed:
        ``guards.dispatch_task`` must not have learned about the incident book.
        """
        task_id = machine.task()
        seed_incident(machine, claude_profile(machine))

        machine.dispatch(task_id)

        assert [row["name"].rsplit("/", 1)[-1] for row in machine.rows()] == [task_id]
