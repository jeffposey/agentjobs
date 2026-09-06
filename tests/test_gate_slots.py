"""The core budget: what one gate may ask for when it is not the only gate on the machine.

Task-339. ``-n auto`` is pytest-xdist for *every core*, and this machine dispatches up to
``limits.max_concurrent_runs`` agents at once, so two gates each claiming all 32 is the
normal case rather than the exception. Task-233 saw it coming and said so in its lever
table -- "Concurrent parallel gates -- not measured" -- and filed nothing; task-336 then
measured its pytest stage at 231 / 250 / 289 / 397 / 451 seconds against the 52 seconds
docs/performance.md documents.

What is guarded here is not the arithmetic -- that is one division -- but the four ways a
budget like this goes wrong:

* it **never blocks and never fails a gate**: every failure is the un-budgeted default;
* a gate running **alone is byte-for-byte what it was**, so the documented single-gate
  timings still describe it;
* **staleness is by age**, because a killed gate cannot clean up after itself and asking
  Windows whether a pid is alive mistakes pid reuse for a running gate;
* the count is taken **when pytest starts**, not when the gate does.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType

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


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(gate_slots.HOME_ENV, str(tmp_path))
    return tmp_path


class TestTheArithmetic:
    """One division, floored, and an escape hatch for the single-gate case."""

    def test_a_lone_gate_asks_for_every_core_exactly_as_before(self) -> None:
        """The single-gate timings in docs/performance.md have to keep describing this."""
        assert gate_slots.workers(cores=32, gates=1) == "auto"

    def test_no_slots_at_all_is_also_auto(self) -> None:
        """An unreadable slot directory reads as zero, and zero must not divide."""
        assert gate_slots.workers(cores=32, gates=0) == "auto"

    def test_two_gates_split_the_machine(self) -> None:
        assert gate_slots.workers(cores=32, gates=2) == "16"

    def test_three_gates_split_it_three_ways(self) -> None:
        """``limits.max_concurrent_runs`` is 3 on this machine, so this is the case."""
        assert gate_slots.workers(cores=32, gates=3) == "10"

    def test_the_division_is_floored_rather_than_trusted(self) -> None:
        """Four cores and three gates divides to one, which is serial pytest -- 431s
        measured against 43s parallel. Below the floor the oversubscription it was
        avoiding is the cheaper problem."""
        assert gate_slots.workers(cores=4, gates=3) == str(gate_slots.MIN_WORKERS)


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

    def test_a_slot_directory_that_cannot_be_written_costs_only_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the gate. This is the whole safety argument for the feature."""
        monkeypatch.setattr(gate_slots, "slots_dir", lambda: Path("\0 not a path"))

        with gate_slots.hold(ROOT):
            pass


class TestStaleness:
    """A killed gate leaves its slot behind, and nobody is there to remove it."""

    def test_a_slot_older_than_the_ceiling_is_ignored(self, home: Path) -> None:
        with gate_slots.hold(ROOT):
            assert gate_slots.active(now=time.time() + gate_slots.STALE_SECONDS + 1) == 0

    def test_and_is_deleted_by_whoever_next_looks(self, home: Path) -> None:
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

    def test_no_directory_at_all_counts_nothing(self, home: Path) -> None:
        assert gate_slots.active() == 0


class TestWhatTheGateDoesWithIt:
    """The join between the budget and the pytest command."""

    @staticmethod
    def only_command(stage: object, **options: int) -> "tuple[list[str], str | None]":
        """A stage's single command. Every stage the budget touches has exactly one."""
        commands, note = check.commands_for(stage, "npm.cmd", **options)
        assert len(commands) == 1
        return commands[0], note

    def test_a_lone_gate_runs_pytest_at_n_auto(self, home: Path) -> None:
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        command, note = self.only_command(stage)

        assert command[command.index("-n") + 1] == "auto"
        assert note is None

    def test_a_gate_with_a_neighbour_asks_for_a_share(self, home: Path) -> None:
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        with gate_slots.hold(ROOT):
            with gate_slots.hold(ROOT):
                command, note = self.only_command(stage)

        value = command[command.index("-n") + 1]
        assert value != "auto" and int(value) >= gate_slots.MIN_WORKERS
        assert note is not None and "-n" in note

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

    def test_a_broken_budget_falls_back_to_the_old_behaviour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every path through ``gate_slots`` lands here as ``-n auto`` -- exactly what
        the gate did before task-339 -- rather than as a failed run."""

        def explode() -> int:
            raise RuntimeError("no home directory")

        monkeypatch.setattr(check.gate_slots, "active", explode)
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        command, note = self.only_command(stage)

        assert command[command.index("-n") + 1] == "auto"
        assert note is None
