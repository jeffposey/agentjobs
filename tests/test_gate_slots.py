"""The gate's share of this machine: how many gates run pytest at once, and how wide.

Task-339 made this a budget -- divide the machine by the number of gates, nobody waits.
Task-513 then measured the worker curve and showed the budget alone has the wrong shape:
the suite is nearly flat from 32 workers to 16 and **3.0x slower at six**, so dividing
without a ceiling does not share the machine, it makes every gate slow at once.
Task-536 therefore adds a capacity of two with a visible queue, an owner's reserve the
suite never gets, and a heartbeat so a long gate cannot vanish from the count.

What is guarded here is not the arithmetic -- that is one division -- but the ways a
regime like this goes wrong:

* it **never blocks forever and never fails a gate**: every failure is a gate that runs;
* **pytest never gets every core**, whether it is a gate or a hand-run ``-n auto``;
* a third gate **queues visibly**, naming who it waits for, rather than looking hung;
* **staleness is by age**, because a killed gate cannot clean up after itself and asking
  Windows whether a pid is alive mistakes pid reuse for a running gate;
* a gate **outliving the old 30-minute ceiling keeps its slot**, which is the specific
  failure that let three gates pile up until one took 45 minutes;
* the count is taken **when pytest starts**, not when the gate does.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, List

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, alias: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(alias, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


gate_slots = load_script("gate_slots", "gate_slots_under_test")
check = load_script("check", "check_under_slots_test")

# The hooks under test in `TestAHandRunPytest` live in the suite's own conftest, which
# pytest has already registered as a plugin by the time this module is imported.
import conftest as suite_conftest  # noqa: E402


@pytest.fixture(autouse=True)
def a_budget_with_no_memory() -> Iterator[None]:
    """Clear the process-wide "this gate has no slot" flag around every test.

    Three copies of ``gate_slots`` are live in a test run -- this module's, the one
    ``check`` imported, and the one ``conftest`` loaded -- and each has its own flag.
    A test that leaves one set changes what the *next* test in this worker computes,
    which is a flake nobody would read as this file's fault.
    """
    for module in (gate_slots, check.gate_slots, suite_conftest.gate_slots):
        if module is not None:
            module.reset_degraded()
    yield
    for module in (gate_slots, check.gate_slots, suite_conftest.gate_slots):
        if module is not None:
            module.reset_degraded()


@pytest.fixture(autouse=True)
def the_default_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here starts at ``CAPACITY``, whatever the gate running it was handed.

    ``CAPACITY_ENV`` is inherited by the pytest a gate launches, which is the point of it
    for ``gate_cost.py`` and ``gate_shape.py``. It also means the suite ran inside a
    three-gate measurement arm read a capacity of three, and six tests here that assert on
    the default of two went red -- which stopped the gate at pytest and cut vitest, build and
    e2e out of the timing being measured (task-534). A test that wants another capacity sets
    it itself.
    """
    monkeypatch.delenv(gate_slots.CAPACITY_ENV, raising=False)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(gate_slots.HOME_ENV, str(tmp_path))
    monkeypatch.delenv(gate_slots.SLOT_ENV, raising=False)
    return tmp_path


def fill(count: int) -> List[Any]:
    """``count`` slots held by gates that are not this test, with no heartbeat thread."""
    return [gate_slots.acquire(ROOT, heartbeat=3600.0) for _ in range(count)]


def free(slots: List[Any]) -> None:
    for slot in slots:
        slot.release()


class FakeClock:
    """A monotonic clock that advances by ``step`` every time it is read.

    Driving the queue needs a clock the test controls, and ``acquire`` reads it a fixed
    number of times per poll, so a self-advancing one is enough to reach any deadline
    without sleeping through it.
    """

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class TestTheArithmetic:
    """One division, capped at the capacity, with six cores that are never the suite's."""

    def test_a_lone_gate_leaves_the_owner_six_cores(self) -> None:
        """Not ``auto``. ``auto`` is every core, on a machine somebody is working on."""
        assert gate_slots.workers(cores=32, gates=1) == "26"

    def test_no_slots_at_all_is_the_lone_case(self) -> None:
        """A directory that exists and is empty means no gate is running, this one
        included -- a gate that has not taken its slot yet still gets the lone share."""
        assert gate_slots.workers(cores=32, gates=0) == "26"

    def test_two_gates_split_what_is_left(self) -> None:
        assert gate_slots.workers(cores=32, gates=2) == "13"

    def test_a_third_gate_gets_the_paired_share_rather_than_a_third(self) -> None:
        """A third gate queues. One that proceeds anyway after the timeout takes what a
        paired gate takes, because a ninth of the machine is where the curve is steep."""
        assert gate_slots.workers(cores=32, gates=3) == "13"
        assert gate_slots.workers(cores=32, gates=9) == "13"

    def test_the_division_is_floored_rather_than_trusted(self) -> None:
        """Eight cores less six, divided between two gates, is one -- which is serial
        pytest, 431s measured against 43s parallel. Below the floor the oversubscription
        being avoided is the cheaper problem."""
        assert gate_slots.workers(cores=8, gates=2) == str(gate_slots.MIN_WORKERS)

    def test_a_machine_smaller_than_the_reserve_still_runs(self) -> None:
        """The reserve would go negative. Nothing here may produce a ``-n -2``."""
        assert gate_slots.workers(cores=4, gates=1) == str(gate_slots.MIN_WORKERS)

    def test_the_concurrent_reserve_comes_off_after_the_division(self) -> None:
        """It is this gate's own cost for the frontend lane beside its suite, not a
        share of the machine -- so a paired concurrent gate pays it once, not twice."""
        assert gate_slots.workers(cores=32, gates=1, reserve=4) == "22"
        assert gate_slots.workers(cores=32, gates=2, reserve=4) == "9"

    def test_the_note_always_says_the_width_and_the_reserve(self) -> None:
        """Every time, not only when the budget bit: a timing in the gate's own table
        cannot be read against a width nobody printed."""
        line = gate_slots.note("13", 2, reserve=0)

        assert "-n 13" in line
        assert str(gate_slots.OWNER_RESERVE) in line
        assert "owner" in line

    def test_the_note_names_the_frontend_lane_when_there_is_one(self) -> None:
        assert "frontend lane" in gate_slots.note("9", 2, reserve=4)


class TestHoldingASlot:
    """A file for as long as a gate runs, and gone afterwards."""

    def test_a_held_slot_is_visible_to_a_neighbour(self, home: Path) -> None:
        with gate_slots.hold(ROOT):
            assert gate_slots.active() == 1

    def test_it_is_released_at_the_end_of_the_block(self, home: Path) -> None:
        with gate_slots.hold(ROOT):
            pass

        assert gate_slots.active() == 0

    def test_it_is_released_when_the_gate_fails(self, home: Path) -> None:
        """A red gate is the common case, not the exceptional one."""
        with pytest.raises(RuntimeError):
            with gate_slots.hold(ROOT):
                raise RuntimeError("stage failed")

        assert gate_slots.active() == 0

    def test_two_gates_are_counted_as_two(self, home: Path) -> None:
        with gate_slots.hold(ROOT):
            with gate_slots.hold(ROOT):
                assert gate_slots.active() == 2

    def test_a_run_that_needs_no_slot_takes_none(self, home: Path) -> None:
        """``--only oxlint`` and ``--serial``. Neither is what the capacity protects,
        and queueing the iteration loop behind two suites would be a bad trade."""
        with gate_slots.hold(ROOT, needed=False):
            assert gate_slots.active() == 0

    def test_the_holder_marks_the_environment_and_puts_it_back(self, home: Path) -> None:
        """How the pytest *inside* a gate knows not to take a second slot."""
        assert not gate_slots.held_by_an_enclosing_gate()

        with gate_slots.hold(ROOT):
            assert gate_slots.held_by_an_enclosing_gate()

        assert not gate_slots.held_by_an_enclosing_gate()

    def test_a_slot_directory_that_cannot_be_written_costs_only_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the gate, and not a wait either. This is the whole safety argument."""
        monkeypatch.setattr(gate_slots, "slots_dir", lambda: Path("\0 not a path"))

        began = time.monotonic()
        with gate_slots.hold(ROOT):
            pass

        assert time.monotonic() - began < 30

    def test_a_gate_with_no_slot_assumes_it_has_company(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is invisible to its neighbours and they are invisible to it, so the honest
        answer is not "I am alone" -- that is two gates at 26 workers on 32 cores."""
        monkeypatch.setattr(gate_slots, "slots_dir", lambda: Path("\0 not a path"))

        with gate_slots.hold(ROOT):
            assert gate_slots.visible_gates() == gate_slots.CAPACITY
            assert gate_slots.workers(cores=32) == "13"


class TestTheCapacity:
    """Two gates run pytest at once here. A third waits, and says who it waits for."""

    def test_a_third_gate_queues_and_names_both_holders(self, home: Path) -> None:
        held = fill(gate_slots.CAPACITY)
        said: List[str] = []

        def sleep(_seconds: float) -> None:
            """The neighbour that finishes while this gate is queued."""
            if held:
                held.pop().release()

        try:
            slot = gate_slots.acquire(
                ROOT, announce=said.append, sleep=sleep, clock=FakeClock(), heartbeat=3600.0
            )
        finally:
            free(held)

        assert slot.path is not None, "the queued gate must start once a slot comes free"
        assert len(said) == 1, "queued once, not once per poll"
        assert "Queued for one of this machine's 2 gate slots" in said[0]
        assert said[0].count("pid ") == gate_slots.CAPACITY, "both holders are named"
        assert "not a stall" in said[0]
        slot.release()

    def test_a_measurement_can_lift_the_capacity_and_says_it_did(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``gate_cost.py`` and ``flake_probe.py`` exist to run a gate at a width it
        would not otherwise be handed. Under a capacity they deadlock -- the third
        synthetic slot queues for a gate that does not exist -- so the ceiling is a
        default rather than a constant, and lifting it also lifts the division's cap."""
        monkeypatch.setenv(gate_slots.CAPACITY_ENV, "5")
        held = fill(4)
        try:
            assert gate_slots.capacity() == 5
            assert gate_slots.active() == 4
            assert gate_slots.workers(cores=32) == "6", "divided by four, not capped at two"
        finally:
            free(held)

    def test_nonsense_in_the_override_is_ignored(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inherited environment is not a reason to run unbounded."""
        for raw in ("", "nonsense", "0", "-3"):
            monkeypatch.setenv(gate_slots.CAPACITY_ENV, raw)
            assert gate_slots.capacity() == gate_slots.CAPACITY

    def test_it_says_so_again_every_thirty_seconds(self, home: Path) -> None:
        """A gate that prints nothing for twenty minutes reads as hung, and the cost of
        that is somebody killing a run that was working."""
        held = fill(gate_slots.CAPACITY)
        said: List[str] = []
        clock = FakeClock(step=gate_slots.QUEUE_NOTICE_SECONDS)
        polls = 0

        def sleep(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls >= 3:
                held.pop().release()

        try:
            slot = gate_slots.acquire(
                ROOT, announce=said.append, sleep=sleep, clock=clock, heartbeat=3600.0
            )
        finally:
            free(held)

        assert len(said) == 3
        assert "Waiting" in said[-1], "a later notice says how long it has been waiting"
        slot.release()

    def test_the_wait_is_bounded_and_the_gate_goes_anyway(self, home: Path) -> None:
        """A budget that can hold a gate forever is a new way to be stuck (task-190)."""
        held = fill(gate_slots.CAPACITY)
        said: List[str] = []
        clock = FakeClock(step=10 * 60.0)

        try:
            slot = gate_slots.acquire(
                ROOT,
                announce=said.append,
                sleep=lambda _s: None,
                clock=clock,
                heartbeat=3600.0,
            )
            assert slot.path is not None, "a bounded wait ends in a gate that runs"
            assert any("stopped waiting" in line for line in said)
            assert gate_slots.active() == gate_slots.CAPACITY + 1
            assert gate_slots.workers(cores=32) == "13", "the paired share, not a third"
            slot.release()
        finally:
            free(held)

    def test_the_acquire_lock_admits_one_gate_at_a_time(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counting the slots and creating one are two operations. Without this, two
        gates arriving together both see the same free slot and both take it."""
        monkeypatch.setattr(gate_slots, "LOCK_ATTEMPTS", 2)
        monkeypatch.setattr(gate_slots, "LOCK_RETRY_SECONDS", 0.0)
        directory = gate_slots.slots_dir()
        directory.mkdir(parents=True, exist_ok=True)

        with gate_slots._exclusive(directory) as first:
            with gate_slots._exclusive(directory) as second:
                assert first is True
                assert second is False, "the second gate must not think it holds the lock"

    def test_a_lock_left_by_a_dead_gate_is_broken_rather_than_obeyed(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing else would ever remove it, and every gate on the machine would then
        acquire unlocked forever -- silently, which is the worst version of this."""
        monkeypatch.setattr(gate_slots, "LOCK_ATTEMPTS", 2)
        monkeypatch.setattr(gate_slots, "LOCK_RETRY_SECONDS", 0.0)
        directory = gate_slots.slots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        lock = directory / gate_slots.LOCK_NAME
        lock.write_text("99999\n", encoding="utf-8")
        aged = time.time() - gate_slots.LOCK_STALE_SECONDS - 5
        os.utime(lock, (aged, aged))

        with gate_slots._exclusive(directory) as taken:
            assert taken is True

    def test_an_unlockable_directory_still_produces_a_gate(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing to take the lock is not failing: the gate proceeds unlocked, which is
        the behaviour of a machine with no lock at all."""
        monkeypatch.setattr(gate_slots, "LOCK_ATTEMPTS", 0)

        with gate_slots.hold(ROOT):
            assert gate_slots.active() == 1


class TestStalenessAndTheHeartbeat:
    """A killed gate leaves its slot behind, and nobody is there to remove it."""

    def test_a_killed_gate_frees_its_slot_within_five_minutes(self, home: Path) -> None:
        """The number that matters to whoever is queued behind it."""
        slot = gate_slots.acquire(ROOT, heartbeat=3600.0)
        killed_at = time.time()

        assert gate_slots.STALE_SECONDS <= 5 * 60
        assert gate_slots.active(now=killed_at + gate_slots.STALE_SECONDS - 1) == 1
        assert gate_slots.active(now=killed_at + gate_slots.STALE_SECONDS + 1) == 0
        slot.release()

    def test_a_gate_running_past_the_old_ceiling_keeps_its_slot(self, home: Path) -> None:
        """The failure this task was filed on. Staleness was thirty minutes and the
        gates ran forty-five: each one vanished from the count while still running, the
        next gate took a larger share of a machine that did not have it, and the pileup
        fed itself."""
        slot = gate_slots.acquire(ROOT, heartbeat=3600.0)
        assert slot.path is not None
        started = time.time()

        for minute in range(1, 46):
            moment = started + minute * 60
            # What the heartbeat thread does, once a minute, for as long as the gate runs.
            os.utime(slot.path, (moment, moment))
            assert gate_slots.active(now=moment) == 1, f"lost its slot at {minute} minutes"

        assert 45 * 60 > 30 * 60, "which is what the old ceiling would have expired"
        slot.release()

    def test_the_heartbeat_thread_really_touches_the_file(self, home: Path) -> None:
        """The assertion above is about the rule; this one is about the thread. Without
        it the rule would be satisfied by a mechanism that never runs."""
        slot = gate_slots.acquire(ROOT, heartbeat=0.05)
        assert slot.path is not None
        try:
            aged = time.time() - 10 * 60
            os.utime(slot.path, (aged, aged))

            deadline = time.time() + 10
            while time.time() < deadline and slot.path.stat().st_mtime <= aged + 1:
                time.sleep(0.05)

            assert slot.path.stat().st_mtime > aged + 1
        finally:
            slot.release()

    def test_the_heartbeat_stops_when_the_slot_is_released(self, home: Path) -> None:
        """A thread that outlived its slot would touch a path another gate may reuse."""
        slot = gate_slots.acquire(ROOT, heartbeat=0.05)
        thread = slot.thread
        slot.release()

        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_a_stale_slot_is_deleted_by_whoever_next_looks(self, home: Path) -> None:
        """Tidied by the next gate that has a reason to look, which is the only moment
        anyone cares whether the directory is tidy."""
        directory = gate_slots.slots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "9999-deadbeef.json").write_text("{}", encoding="utf-8")

        gate_slots.active(now=time.time() + gate_slots.STALE_SECONDS + 1)

        assert list(directory.glob("*.json")) == []

    def test_a_sweep_takes_the_corpse_and_leaves_the_live_gate(self, home: Path) -> None:
        """The failure that matters: a sweep is triggered by a running gate, so one that
        took its own slot with the corpse would divide by the wrong number and, worse,
        delete the file its neighbour is relying on."""
        directory = gate_slots.slots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        corpse = directory / "9999-deadbeef.json"
        corpse.write_text("{}", encoding="utf-8")
        aged = time.time() - gate_slots.STALE_SECONDS - 60
        os.utime(corpse, (aged, aged))

        with gate_slots.hold(ROOT):
            assert gate_slots.active() == 1

        assert not corpse.exists()

    def test_no_directory_at_all_counts_nothing_and_reads_as_readable(self, home: Path) -> None:
        """A directory nothing has created yet means no gate is running. A directory
        that cannot be read means nothing at all, and the two must not divide alike."""
        found = gate_slots.survey()

        assert found.gates == 0
        assert found.readable is True

    def test_a_slot_file_written_by_an_older_gate_still_counts(self, home: Path) -> None:
        """Two gates from different commits share this directory during a rebase."""
        directory = gate_slots.slots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "1234-abcdef01.json").write_text("{}", encoding="utf-8")

        found = gate_slots.survey()

        assert found.gates == 1
        assert "pid 0" in found.holders[0].describe()


class TestWhatTheGateDoesWithIt:
    """The join between the budget and the pytest command."""

    @staticmethod
    def only_command(stage: object, **options: int) -> "tuple[list[str], str | None]":
        """A stage's single command. Every stage the budget touches has exactly one."""
        commands, note = check.commands_for(stage, "npm.cmd", **options)
        assert len(commands) == 1
        return commands[0], note

    def test_a_lone_gate_runs_pytest_at_the_lone_share(self, home: Path) -> None:
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        command, note = self.only_command(stage)

        value = command[command.index("-n") + 1]
        assert value != "auto"
        assert int(value) == check.gate_slots.budget()
        assert note is not None and f"-n {value}" in note

    def test_a_gate_with_a_neighbour_asks_for_less(self, home: Path) -> None:
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        alone, _ = self.only_command(stage)
        with gate_slots.hold(ROOT):
            with gate_slots.hold(ROOT):
                paired, note = self.only_command(stage)

        lone_value = int(alone[alone.index("-n") + 1])
        paired_value = int(paired[paired.index("-n") + 1])
        assert paired_value < lone_value
        assert paired_value >= check.gate_slots.MIN_WORKERS
        assert note is not None and "2 gates" in note

    def test_no_other_stage_carries_the_token(self, home: Path) -> None:
        """A stage whose argv still held ``@workers`` would be run with a literal
        ``@workers`` on the command line, which is the one way this could break a gate
        that is not pytest."""
        for stage in check.stages():
            for command in check.commands_for(stage, "npm.cmd")[0]:
                assert check.WORKERS_TOKEN not in command

    def test_serial_carries_no_token_and_needs_no_budget(self, home: Path) -> None:
        stage = next(stage for stage in check.stages(parallel=False) if stage.name == "pytest")

        command, note = self.only_command(stage)

        assert "-n" not in command
        assert note is None

    def test_a_broken_budget_falls_back_to_the_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """To the floor rather than to ``-n auto``: every core is the behaviour the
        owner's reserve exists to prevent, so it is not available as a fallback."""

        def explode() -> int:
            raise RuntimeError("no home directory")

        monkeypatch.setattr(check.gate_slots, "visible_gates", explode)
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        command, note = self.only_command(stage)

        assert command[command.index("-n") + 1] == str(check.gate_slots.MIN_WORKERS)
        assert note is None


def fake_config(*, numprocesses: Any = None, collectonly: bool = False) -> Any:
    """Enough of ``pytest.Config`` for the hooks in the suite's conftest.

    Returned as ``Any`` rather than typed: building a real ``Config`` takes a whole
    ``pytest.main`` startup, and the two attributes these hooks read are the two this
    has.
    """

    class FakeConfig:
        option = type("Options", (), {"numprocesses": numprocesses})()

        def getoption(self, name: str, default: Any = None) -> Any:
            return collectonly if name == "collectonly" else default

    return FakeConfig()


class TestAHandRunPytest:
    """``pytest -n auto`` by hand obeys the same rule, through the conftest hook.

    ``gate_slots`` only ever saw ``scripts/check.py``, so an agent running the suite by
    hand took all 32 cores and was invisible to every gate on the machine while doing it.
    """

    def test_dash_n_auto_resolves_to_the_budget_rather_than_every_core(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # As if inside a gate, so the hook resolves a count without taking a slot.
        monkeypatch.setenv(gate_slots.SLOT_ENV, "held-by-the-gate")

        resolved = suite_conftest.pytest_xdist_auto_num_workers(fake_config(numprocesses="auto"))

        assert resolved == suite_conftest.gate_slots.budget()
        assert resolved < (os.cpu_count() or 1)

    def test_a_parallel_run_outside_a_gate_wants_a_slot(self, home: Path) -> None:
        assert suite_conftest.a_slot_is_wanted(fake_config(numprocesses=8), workers=8)

    def test_a_serial_run_takes_nothing_and_waits_for_nothing(self, home: Path) -> None:
        """``pytest -k one_test`` costs one core and must never queue behind a gate."""
        assert not suite_conftest.a_slot_is_wanted(fake_config(), workers=0)
        assert not suite_conftest.a_slot_is_wanted(fake_config(numprocesses=1), workers=1)

    def test_the_pytest_inside_a_gate_does_not_take_a_second_slot(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate is already holding one on its behalf, and one gate counted twice
        would halve its own suite."""
        monkeypatch.setenv(gate_slots.SLOT_ENV, "held-by-the-gate")

        assert not suite_conftest.a_slot_is_wanted(fake_config(numprocesses=8), workers=8)

    def test_an_xdist_worker_takes_nothing(self, home: Path) -> None:
        """It is one of the processes the controller's slot already paid for."""
        config = fake_config(numprocesses=8)
        config.workerinput = {}

        assert not suite_conftest.a_slot_is_wanted(config, workers=8)

    def test_collection_only_takes_nothing(self, home: Path) -> None:
        """It starts no workers whatever ``-n`` says."""
        assert not suite_conftest.a_slot_is_wanted(
            fake_config(numprocesses=8, collectonly=True), workers=8
        )

    def test_a_hand_run_that_takes_a_slot_gives_it_back(self, home: Path) -> None:
        """Held for the session and released at ``pytest_unconfigure``; a missed release
        expires within ``STALE_SECONDS`` rather than throttling the machine."""
        config = fake_config(numprocesses=8)
        try:
            suite_conftest.take_a_slot(config, workers=8)
            assert gate_slots.active() == 1
        finally:
            suite_conftest.pytest_unconfigure(config)

        assert gate_slots.active() == 0


def test_the_reserve_and_the_capacity_are_the_documented_ones() -> None:
    """The numbers ENGINEERING.md and docs/performance.md quote, asserted once so the
    prose and the code cannot drift apart silently."""
    assert gate_slots.CAPACITY == 2
    assert gate_slots.OWNER_RESERVE == 6
    assert gate_slots.workers(cores=32, gates=1) == "26"
    assert gate_slots.workers(cores=32, gates=2) == "13"


def test_every_public_entry_point_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may raise into a gate, whatever the filesystem does.

    The one property that is not negotiable: a slot is an optimisation and the gate is
    not, so a machine that cannot keep slot files runs slow gates rather than no gates.
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("the disk is gone")

    monkeypatch.setattr(gate_slots.Path, "iterdir", explode)
    monkeypatch.setattr(gate_slots.Path, "mkdir", explode)
    monkeypatch.setattr(gate_slots.Path, "write_text", explode)

    assert gate_slots.active() == 0
    assert gate_slots.visible_gates() == gate_slots.CAPACITY
    assert int(gate_slots.workers(cores=32)) >= gate_slots.MIN_WORKERS
    with gate_slots.hold(ROOT):
        pass
