"""The gate's order, its selection flags, and the two things it must never become.

Task-189. A session working task-188 ran ``scripts/check.py`` four times for about
sixteen minutes to extract three failures. Two of the four runs paid a four-minute
pytest stage to reach a check that knew its answer in a second, and one of those two
was the same failure twice, because the check's remedy named only half of what it
wanted.

The properties guarded here are the ones that make that not happen again: cheap stages
run first, a late failure can be resumed from, and neither flag can turn into a way of
running less than the whole gate when the whole gate is what was asked for.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a repository script by path, without making ``scripts/`` a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = load_script("check")


def names(stages: list[object]) -> list[str]:
    return [stage.name for stage in stages]  # type: ignore[attr-defined]


def command_count(stages: list[Any]) -> int:
    """How many child processes a run of these stages starts.

    Not the same as the number of stages since task-268: ``api`` exports the OpenAPI
    document and then compares the generated client against it, as two commands the gate
    runs itself rather than one ``npm run check:api`` that starts a nested Poetry.
    """
    return sum(len(stage.steps) for stage in stages)


@pytest.fixture(autouse=True)
def no_receipt_from_a_simulated_gate(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Stop the tests below issuing a gate receipt for this repository.

    Several of them call ``check.main([])`` with ``subprocess.run`` stubbed, so a full
    green run is reported having executed nothing. Left alone, that would write a
    receipt attesting that this checkout's gate passed at HEAD, and a later
    ``--since-gate`` run would trust it -- a soundness hole dug by the test suite, in
    the one feature whose whole point is that its evidence is real.

    Autouse rather than opt-in, for the reason autouse fixtures usually are: forgetting
    it produces no failure, only a false receipt in a directory nobody looks at.

    It also stands in for the two git queries a receipt needs. Those go through the same
    ``subprocess.run`` the tests stub to record stage commands -- ``check.subprocess``
    and ``gate_scope.subprocess`` are one module object -- so leaving them real would put
    ``git rev-parse`` into the list of stages a test is counting.
    """
    written: list[object] = []

    def record(root: object, commit: str, *, basis: object) -> object:
        written.append((commit, basis))
        return Path("receipt")

    monkeypatch.setattr(check.gate_scope, "write_receipt", record)
    monkeypatch.setattr(check.gate_scope, "head_commit", lambda root: "b" * 40)
    monkeypatch.setattr(check.gate_scope, "tree_is_clean", lambda root: True)
    # Task-339 added two more git queries on the same path -- the tree fingerprint the
    # ALREADY GREEN notice compares on, and the dirty paths a missing receipt names --
    # and they go through the same stubbed ``subprocess.run``. Left real, each one adds
    # commands to the list a test is counting, and the count is the assertion.
    monkeypatch.setattr(check.gate_scope, "tree_fingerprint", lambda root: "fingerprint")
    monkeypatch.setattr(check.gate_scope, "dirty_paths", lambda root: [])
    return written


# --- the order ----------------------------------------------------------------------


class TestOrder:
    """Cheap first. The rule, and the two dependencies that qualify it."""

    def test_the_second_long_checks_all_run_before_pytest(self) -> None:
        """The defect itself: hygiene checks stranded behind a four-minute suite.

        Named individually rather than as "everything under twenty seconds", because a
        threshold this test cannot measure is a threshold it cannot enforce -- the
        timings live in ENGINEERING.md, and the list of what they justify lives here.
        """
        order = names(check.stages())
        pytest_at = order.index("pytest")

        for cheap in ("black", "ruff", "mypy", "api", "icons", "oxlint"):
            assert order.index(cheap) < pytest_at, f"{cheap} must not wait for pytest"

    def test_the_build_runs_before_the_browser_tests_it_serves(self) -> None:
        """A real dependency, not habit: Playwright drives the bundle ``build`` writes."""
        order = names(check.stages())
        assert order.index("build") < order.index("e2e")

    def test_the_openapi_document_is_exported_before_a_client_is_compared_to_it(self) -> None:
        """``api`` is one stage precisely because its two halves are ordered."""
        api = next(stage for stage in check.stages() if stage.name == "api")

        assert [step[0] for step in api.steps] == [check.PYTHON, check.NPM]
        assert "export_openapi.py" in api.steps[0][1]
        assert api.steps[1][-1] == "check:api-client"

    def test_every_stage_has_a_distinct_name(self) -> None:
        """``--only`` addresses stages by name, so two stages sharing one is a bug."""
        order = names(check.stages())
        assert len(set(order)) == len(order)


# --- selection ----------------------------------------------------------------------


class TestSelection:
    """``--only`` and ``--from`` exist for the loop between a late failure and its fix."""

    def test_no_selection_runs_everything(self) -> None:
        assert names(check.select(check.stages(), [], None)) == names(check.stages())

    def test_from_runs_the_named_stage_and_the_rest(self) -> None:
        selected = names(check.select(check.stages(), [], "vitest"))
        assert selected[0] == "vitest"
        assert selected == names(check.stages())[-3:]

    def test_only_runs_just_what_was_named(self) -> None:
        assert names(check.select(check.stages(), ["mypy", "e2e"], None)) == ["mypy", "e2e"]

    def test_only_keeps_the_table_s_order_whatever_order_was_asked(self) -> None:
        """Otherwise ``--only build,e2e`` and ``--only e2e,build`` mean different things,
        and one of them runs Playwright against a bundle that has not been built yet."""
        assert names(check.select(check.stages(), ["e2e", "build"], None)) == ["build", "e2e"]

    def test_an_unknown_stage_is_refused_rather_than_skipped(self) -> None:
        """A typo that selected nothing would report a green gate that ran no checks."""
        with pytest.raises(ValueError) as raised:
            check.select(check.stages(), ["pytests"], None)

        assert "pytests" in str(raised.value)
        # The remedy is the list of real names; a reader should not have to go looking.
        assert "pytest" in str(raised.value)

    def test_an_unknown_stage_is_refused_for_from_too(self) -> None:
        with pytest.raises(ValueError):
            check.select(check.stages(), [], "nonsense")

    def test_the_flags_cannot_be_combined(self) -> None:
        """They would have to mean something, and nothing they could mean is useful."""
        with pytest.raises(SystemExit):
            check.parse_args(["--only", "mypy", "--from", "pytest"])

    def test_only_accepts_a_comma_separated_list(self) -> None:
        assert check.parse_args(["--only", "mypy,e2e"]).only == ["mypy", "e2e"]

    def test_only_is_repeatable(self) -> None:
        assert check.parse_args(["--only", "mypy", "--only", "e2e"]).only == ["mypy", "e2e"]


# --- what the gate still is ---------------------------------------------------------


class TestTheUnqualifiedGate:
    """The constraint the flags are allowed to exist under."""

    @staticmethod
    def record_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        commands: list[list[str]] = []

        def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(check.subprocess, "run", record)
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")
        return commands

    def test_the_bare_command_runs_every_stage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The commit rule in ENGINEERING.md points at this invocation and no other."""
        commands = self.record_runs(monkeypatch)

        assert check.main([]) == 0
        assert len(commands) == command_count(check.stages())

    def test_a_selection_runs_only_what_was_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands = self.record_runs(monkeypatch)

        assert check.main(["--only", "black"]) == 0
        assert len(commands) == 1

    def test_listing_the_stages_runs_none_of_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands = self.record_runs(monkeypatch)

        assert check.main(["--list"]) == 0
        assert commands == []

    # What each frontend-facing stage of the gate covers of ``npm run check``. Written
    # down because since task-268 two of them no longer *are* the npm script: the gate
    # runs ``check:api-schema`` and ``check:icons`` as Python itself, to avoid a nested
    # ``poetry run``. The map is compared against package.json below, so a check added to
    # the frontend gate and not to this one still fails.
    FRONTEND_COVERAGE = {
        "api": ("check:api",),
        "icons": ("check:icons",),
        "oxlint": ("lint",),
        "vitest": ("test",),
        "build": ("build",),
        "e2e": ("test:e2e",),
    }

    def test_the_frontend_stages_are_exactly_the_frontend_gate(self) -> None:
        """Two descriptions of one order drift apart, so assert they agree.

        ``npm run check`` is documented as the frontend gate and is run on its own.
        Since task-189 the stage table is the authority on order, but a stage added to
        one and not the other is a check that silently stops running in the gate that
        matters.
        """
        manifest = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        scripted = re.findall(r"npm run ([\w:-]+)", manifest["scripts"]["check"])
        covered = [script for scripts in self.FRONTEND_COVERAGE.values() for script in scripts]
        in_the_table = names([s for s in check.stages() if s.cwd == ROOT / "frontend"])

        assert sorted(scripted) == sorted(covered)
        assert sorted(in_the_table) == sorted(self.FRONTEND_COVERAGE)

    def test_the_gate_starts_no_nested_poetry_run(self) -> None:
        """Task-268's ac-3, asserted where it can regress: the stage table.

        ``check:api-schema`` and ``check:icons`` are ``poetry run python ...`` in
        package.json, so reaching them through npm started npm, then Poetry, then a
        third interpreter -- and every ``poetry run`` is a fresh chance for Poetry to
        prefer an *activated* virtualenv over this checkout's, which is task-210.
        Playwright's ``webServer`` is the one that remains, and it is out of the stage
        table's reach.
        """
        for stage in check.stages():
            for step in stage.steps:
                assert "poetry" not in " ".join(step), f"{stage.name} nests a poetry run"


# --- the pytest stage's two options -------------------------------------------------


class TestPytestOptions:
    """Coverage and parallelism, both moved out of ``addopts`` by task-233.

    The suite is 89% of the gate. Serial with coverage it was 540s on this machine;
    serial without, 343s; at ``-n auto``, 43s -- same commit, same 2538 passing. The
    numbers are in docs/performance.md; what is guarded here is that the flags still mean
    what those numbers were measured under.

    Since task-339 the ``-n`` *value* is a placeholder here and is resolved when pytest is
    about to run, from how many gates share the machine -- see ``tests/test_gate_slots.py``.
    These tests assert on the flag's presence for that reason, not by accident.
    """

    @staticmethod
    def pytest_args(**options: bool) -> list[str]:
        stage = next(s for s in check.stages(**options) if s.name == "pytest")
        return [str(arg) for step in stage.steps for arg in step]

    def test_the_gate_runs_the_suite_in_parallel_by_default(self) -> None:
        assert "-n" in self.pytest_args()

    def test_the_gate_does_not_measure_coverage_by_default(self) -> None:
        """It cost minutes per run and wrote a report nobody opens before a commit."""
        assert not any(arg.startswith("--cov") for arg in self.pytest_args())

    def test_coverage_is_available_on_request(self) -> None:
        args = self.pytest_args(coverage=True)

        assert "--cov=src/agentjobs" in args

    def test_serial_is_available_for_reading_a_failure(self) -> None:
        """xdist interleaves output, which is the wrong trade while debugging one test."""
        assert "-n" not in self.pytest_args(parallel=False)

    def test_the_options_touch_no_other_stage(self) -> None:
        plain = {s.name: s.steps for s in check.stages()}
        loud = {s.name: s.steps for s in check.stages(coverage=True, parallel=False)}

        assert [name for name in plain if plain[name] != loud[name]] == ["pytest"]

    def test_the_gate_always_asks_for_the_slowest_tests(self) -> None:
        """Task-268's ac-2. Every proposal about this suite has been arithmetic over the
        total, with no per-test measurement in the repository to check it against."""
        assert "--durations=15" in self.pytest_args()

    def test_the_durations_survive_serial_and_coverage(self) -> None:
        """Serial is the honest attribution, so it is the last place to lose the flag."""
        assert "--durations=15" in self.pytest_args(parallel=False)
        assert "--durations=15" in self.pytest_args(coverage=True)

    def test_addopts_no_longer_forces_coverage_on_every_pytest_invocation(self) -> None:
        """The config change is the saving; the flag above is only how you opt back in."""
        config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        directives = [
            line for line in config.splitlines() if line.startswith("addopts") and "--cov" in line
        ]

        assert directives == []

    def test_xdist_is_a_declared_dependency_rather_than_something_installed_by_hand(
        self,
    ) -> None:
        config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        assert "pytest-xdist" in config


# --- the necessity rule -------------------------------------------------------------


class TestSinceGate:
    """``--since-gate`` is an exception to the commit rule, so it is fenced in code too.

    The derivation itself is tested in ``tests/test_gate_scope.py``. What matters here
    is that the exception cannot be smuggled into the other flags, and that a run using
    it is still unmistakably not the gate.
    """

    def test_it_cannot_be_combined_with_a_manual_selection(self) -> None:
        """Otherwise 'derived' and 'asserted' would be mixable in one invocation."""
        with pytest.raises(SystemExit):
            check.parse_args(["--since-gate", "--only", "pytest"])

    def test_it_cannot_be_combined_with_from(self) -> None:
        with pytest.raises(SystemExit):
            check.parse_args(["--since-gate", "--from", "pytest"])

    def test_without_a_receipt_it_runs_every_stage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Refusing to narrow is the safe direction, and it says why."""
        commands = TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.setattr(check.gate_scope, "read_receipt", lambda root: None)

        assert check.main(["--since-gate"]) == 0
        assert len(commands) == command_count(check.stages())
        assert "FULL GATE" in capsys.readouterr().out

    def test_a_narrowed_run_runs_only_those_stages_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commands = TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.setattr(check.gate_scope, "read_receipt", lambda root: {"commit": "a" * 40})
        monkeypatch.setattr(
            check.gate_scope, "changed_since", lambda root, commit: ["tasks/p/task-1.yaml"]
        )

        assert check.main(["--since-gate"]) == 0
        assert len(commands) == 1
        out = capsys.readouterr().out
        assert "NECESSITY RUN" in out
        assert "Ran every stage" not in out

    def test_an_unchanged_tree_runs_nothing_and_claims_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commands = TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.setattr(check.gate_scope, "read_receipt", lambda root: {"commit": "a" * 40})
        monkeypatch.setattr(check.gate_scope, "changed_since", lambda root, commit: [])

        assert check.main(["--since-gate"]) == 0
        assert commands == []
        assert "NOTHING CHANGED" in capsys.readouterr().out


# --- receipts -----------------------------------------------------------------------


class TestReceiptsAreEarned:
    """Only a run that skipped nothing it was not entitled to skip may issue one."""

    def test_a_full_green_run_issues_one(
        self, monkeypatch: pytest.MonkeyPatch, no_receipt_from_a_simulated_gate: list[object]
    ) -> None:
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        assert check.main([]) == 0
        assert no_receipt_from_a_simulated_gate == [("b" * 40, None)]

    def test_a_selection_never_issues_one(
        self, monkeypatch: pytest.MonkeyPatch, no_receipt_from_a_simulated_gate: list[object]
    ) -> None:
        """The same rule PARTIAL RUN states: a partial green is not the gate's green."""
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        assert check.main(["--only", "black"]) == 0
        assert no_receipt_from_a_simulated_gate == []

    def test_a_narrowed_run_issues_one_that_records_what_it_derived_from(
        self, monkeypatch: pytest.MonkeyPatch, no_receipt_from_a_simulated_gate: list[object]
    ) -> None:
        TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.setattr(check.gate_scope, "read_receipt", lambda root: {"commit": "a" * 40})
        monkeypatch.setattr(
            check.gate_scope, "changed_since", lambda root, commit: ["tasks/p/task-1.yaml"]
        )

        assert check.main(["--since-gate"]) == 0
        assert no_receipt_from_a_simulated_gate == [("b" * 40, "a" * 40)]

    def test_a_dirty_tree_earns_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_receipt_from_a_simulated_gate: list[object],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A receipt names a commit, and a dirty tree is not one."""
        TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.setattr(check.gate_scope, "tree_is_clean", lambda root: False)

        assert check.main([]) == 0
        assert no_receipt_from_a_simulated_gate == []
        assert "working tree is dirty" in capsys.readouterr().out

    def test_a_failing_run_earns_nothing(
        self, monkeypatch: pytest.MonkeyPatch, no_receipt_from_a_simulated_gate: list[object]
    ) -> None:
        TestReporting.fail_at(monkeypatch, "mypy")

        assert check.main([]) != 0
        assert no_receipt_from_a_simulated_gate == []


# --- phase records ------------------------------------------------------------------


class TestPhaseRecords:
    """The gate is the largest phase of a dispatched run, and the one that can time itself.

    Before task-233 a run's only durable artefacts were a start time, a finish time and a
    TTY capture from which no phase attribution survives. These records are what
    ``scripts/run_report.py`` reads.
    """

    @staticmethod
    def in_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from agentjobs.dispatch.phases import RUN_DIR_ENV, RUN_ID_ENV

        monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
        monkeypatch.setenv(RUN_ID_ENV, "run_test")
        return tmp_path

    def test_a_green_run_records_a_start_and_a_finish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.dispatch.phases import read_phases

        TestTheUnqualifiedGate.record_runs(monkeypatch)
        directory = self.in_a_run(tmp_path, monkeypatch)

        assert check.main([]) == 0

        records = read_phases(directory)
        kinds = [record["kind"] for record in records]
        # A start, a finish, and one pair per stage in between (task-321). The pairs are
        # what lets a watcher say "pytest, 7 of 10" while a scripted finish is in its
        # gate, which is the better part of three minutes with nothing else to report.
        assert kinds[0] == "gate_started"
        assert kinds[-1] == "gate_finished"
        assert set(kinds[1:-1]) == {"gate_stage_started", "gate_stage_finished"}
        stages = [record["stage"] for record in records if record["kind"] == "gate_stage_finished"]
        assert stages == [stage.name for stage in check.stages()]
        # Every stage says where it is in the run, so a reader never has to count.
        assert [
            record["index"] for record in records if record["kind"] == "gate_stage_started"
        ] == [position for position in range(1, len(stages) + 1)]

    def test_the_finish_says_whether_it_passed_and_what_it_cost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.dispatch.phases import read_phases

        TestTheUnqualifiedGate.record_runs(monkeypatch)
        directory = self.in_a_run(tmp_path, monkeypatch)

        check.main([])

        finished = read_phases(directory)[-1]
        assert finished["passed"] is True
        assert finished["scope"] == "full"
        assert finished["stages_run"] == len(check.stages())
        assert isinstance(finished["seconds"], (int, float))

    def test_a_failed_gate_names_the_stage_that_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that pays for four failed gates should be visible as exactly that."""
        from agentjobs.dispatch.phases import read_phases

        TestReporting.fail_at(monkeypatch, "mypy")
        directory = self.in_a_run(tmp_path, monkeypatch)

        assert check.main([]) != 0

        finished = read_phases(directory)[-1]
        assert finished["passed"] is False
        assert finished["failed_stage"] == "mypy"

    def test_the_stages_are_not_told_they_are_inside_a_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate records itself. A stage that inherited the pair would record more.

        `pytest` is the case that bites: it runs this repository's own tests of
        `check.main`, so run inside a real dispatched run each simulated gate appended a
        record to that run's ledger -- sixteen phantom gate runs beside one true one, the
        first time the gate was run inside a run directory.
        """
        commands: list[dict[str, str]] = []

        def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            commands.append(kwargs["env"])  # type: ignore[arg-type]
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(check.subprocess, "run", record)
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")
        self.in_a_run(tmp_path, monkeypatch)

        assert check.main([]) == 0

        assert len(commands) == command_count(check.stages())
        for env in commands:
            assert "AGENTJOBS_RUN_DIR" not in env
            assert "AGENTJOBS_RUN_ID" not in env

    def test_outside_a_run_nothing_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer running the gate by hand is not being measured."""
        from agentjobs.dispatch.phases import PHASES_FILENAME, RUN_DIR_ENV

        TestTheUnqualifiedGate.record_runs(monkeypatch)
        monkeypatch.delenv(RUN_DIR_ENV, raising=False)

        assert check.main([]) == 0
        assert not (tmp_path / PHASES_FILENAME).exists()


# --- reporting ----------------------------------------------------------------------


class TestReporting:
    """A failure has to say what it cost and how to pick up from it."""

    @staticmethod
    def fail_at(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
        wanted = next(s for s in check.stages() if s.name == stage)
        # Everything after the runner, which is the only part a stage chooses. A stage
        # may be more than one command, and failing its first is the honest simulation.
        tails = [list(step[1:]) for step in wanted.steps]

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if command[1:] in tails:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(check.subprocess, "run", run)
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")

    def test_a_late_failure_says_how_to_resume(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole point: nine of those sixteen minutes were a green suite re-running."""
        self.fail_at(monkeypatch, "e2e")

        assert check.main([]) == 1
        assert "--from e2e" in capsys.readouterr().err

    def test_a_failure_in_the_first_stage_does_not(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is nothing above it to skip, so the advice would be noise."""
        self.fail_at(monkeypatch, "black")

        assert check.main([]) == 1
        assert "--from" not in capsys.readouterr().err

    def test_every_run_prints_what_each_stage_cost(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-measuring the budget in ENGINEERING.md should not need instrumentation."""
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        check.main([])

        printed = capsys.readouterr().out
        for stage in check.stages():
            assert re.search(rf"^  {stage.name} +\d+\.\d+s$", printed, re.MULTILINE)
        assert re.search(r"^  total +\d+\.\d+s$", printed, re.MULTILINE)

    def test_a_full_run_says_it_ran_everything(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        check.main([])

        printed = capsys.readouterr().out
        assert "Ran every stage" in printed
        assert "PARTIAL RUN" not in printed

    def test_a_partial_run_cannot_be_read_as_a_full_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The real hazard in `--from`: a green that means less than it looks like.

        An agent that resumes at `e2e`, sees no failure and reports "the gate passed"
        has run one stage of ten. So the run says how many it ran, names the ones it
        did not, and says outright that it is not the gate -- at the end as well as the
        start, because the start is thousands of lines of output away by then.
        """
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        assert check.main(["--from", "e2e"]) == 0

        printed = capsys.readouterr().out
        assert "PARTIAL RUN: 1 of 10" in printed
        assert "pytest" in printed  # named among the stages it skipped
        assert "not the gate" in printed
        assert printed.count("PARTIAL RUN") == 2
        assert "Ran every stage" not in printed

    def test_a_failed_run_still_prints_the_timings_it_has(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.fail_at(monkeypatch, "mypy")

        check.main([])

        printed = capsys.readouterr().out
        timed = re.findall(r"^  (\w+) +\d+\.\d+s$", printed, re.MULTILINE)

        assert timed == ["black", "ruff", "mypy", "total"]


# --- and the documentation says so --------------------------------------------------


class TestWhatEngineeringMdMustStillSay:
    """The order and the commit rule are decisions, and a decision drifts if nothing
    holds it. These assert the two that were expensive to reach, not the prose."""

    @staticmethod
    def handbook() -> str:
        return (ROOT / "ENGINEERING.md").read_text(encoding="utf-8")

    def test_every_stage_is_named_where_the_handbook_sends_a_reader(self) -> None:
        """A stage nothing names is one nobody can ask for by name.

        The list moved to docs/performance.md in task-339 -- the four always-loaded files
        have a byte budget and a table of measured costs is a poor use of it -- so what is
        asserted is the pair: the names are somewhere, and the handbook points there.
        """
        costs = (ROOT / "docs" / "performance.md").read_text(encoding="utf-8")
        for stage in check.stages():
            assert f"`{stage.name}`" in costs, f"docs/performance.md never names {stage.name}"
        assert "docs/performance.md#what-the-gate-costs" in self.handbook()

    def test_the_selection_flags_are_documented_as_not_being_the_gate(self) -> None:
        text = self.handbook()
        assert "--from" in text and "--only" in text
        assert "PARTIAL RUN" in text

    def test_the_gate_before_commit_contradiction_stays_resolved(self) -> None:
        """task-189 decided which side wins. Losing the sentence loses the decision.

        The decision is that **no stage of the gate may require a commit**: the two
        generated checks compare against the working tree, so the files have to be on
        disk and need not be committed. Task-339 changed the *advice* that used to be
        bolted onto it -- "run the gate, then commit" -- because a receipt is only issued
        for a tree that is a commit, and gating the committed rebased branch once is what
        earns one. The two do not conflict, and this asserts the half that is a decision.

        Whitespace is collapsed before matching: this asserts the decision, not the
        column the paragraph happens to wrap at. It used to pin the line break too, and
        task-305 rewrapped the paragraph without touching a word of the rule -- which
        failed the gate and said nothing useful about why.
        """
        text = " ".join(self.handbook().replace("**", "").split())
        assert "No stage of the gate may require a commit" in text
        assert "regenerate before you gate" in text
        assert "compare against the working tree, never `HEAD`" in text

    def test_the_one_gate_sequence_is_stated_in_order(self) -> None:
        """task-339. The rule is the *order*, so a summary that loses it is not the rule.

        Runs launched the gate six to nine times each and half of those went red; three
        of task-336's eight launches are removed by iterating with ``--only`` while a
        stage is red and gating the committed, rebased branch exactly once.
        """
        text = " ".join(self.handbook().replace("**", "").split())
        assert "#### One gate per handoff" in self.handbook()
        assert "A branch is gated once" in text
        for step in ("iterate with `--only", "Commit, then rebase onto `main`", "Hand off"):
            assert step in text, step
        # And the sentence that used to read as one gate per commit.
        assert "not a gate per commit" in text


# --- concurrency, which is a flag and not a default ---------------------------------


class TestConcurrentStages:
    """``--concurrent`` (task-268), and the reasons it is off.

    The gate's whole value is that its green means something, and every property that
    makes concurrency fast is also a way to make a green unreliable -- two suites over 32
    cores is what task-339 measured driving this machine to 6MB free, and Playwright's
    30s bounds are what an oversubscribed machine breaks. So what is asserted here is
    that it stays opt-in, that its scheduling respects the two real dependencies, and
    that a failure inside it is still a failure.
    """

    @staticmethod
    def record_runs(monkeypatch: pytest.MonkeyPatch, fails: str = "") -> list[list[str]]:
        """Capture every command a run starts, in the order it started them.

        ``fails`` names a substring; a command containing it exits non-zero. The
        concurrent runner captures output rather than raising, so this returns a
        completed process either way -- which is also what the real thing does.
        """
        started: list[list[str]] = []
        lock = threading.Lock()

        def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            with lock:
                started.append(command)
            joined = " ".join(command)
            code = 1 if fails and fails in joined else 0
            return subprocess.CompletedProcess(command, code, "", "")

        monkeypatch.setattr(check.subprocess, "run", record)
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")
        return started

    @staticmethod
    def first_index(started: list[list[str]], needle: str) -> int:
        return next(i for i, command in enumerate(started) if needle in " ".join(command))

    def test_it_is_off_unless_asked_for(self) -> None:
        """The unqualified ``scripts/check.py`` is what the commit rule names, and it
        must keep meaning the thing docs/performance.md measured."""
        assert check.parse_args([]).concurrent is False

    def test_it_cannot_be_combined_with_serial(self) -> None:
        """``--serial`` is asked for when interleaved output is the problem."""
        with pytest.raises(SystemExit):
            check.parse_args(["--concurrent", "--serial"])

    def test_the_declared_dependencies_agree_with_the_serial_order(self) -> None:
        """One graph, written twice: as positions in ``stages()`` and as
        ``DEPENDENCIES``. A concurrent run reads the second, so they have to agree."""
        order = names(check.stages())
        for stage, needs in check.DEPENDENCIES.items():
            for dependency in needs:
                assert order.index(dependency) < order.index(stage), f"{stage} needs {dependency}"

    def test_only_the_two_real_dependencies_are_declared(self) -> None:
        """Everything else is independent, and a dependency nobody needs is wall clock
        given away for nothing."""
        assert check.DEPENDENCIES == {"vitest": ("api",), "build": ("api",), "e2e": ("build",)}

    def test_a_dependency_outside_the_selection_is_not_waited_for(self) -> None:
        """``--only vitest`` asks for one stage. Blocking it on an ``api`` nobody
        selected would hang rather than run."""
        vitest = next(stage for stage in check.stages() if stage.name == "vitest")

        assert check.ready([vitest], set(), {"vitest"}) == [vitest]
        assert check.ready([vitest], set(), {"vitest", "api"}) == []

    def test_a_concurrent_run_still_runs_every_stage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started = self.record_runs(monkeypatch)

        assert check.main(["--concurrent"]) == 0
        assert len(started) == command_count(check.stages())

    def test_the_writing_stage_finishes_before_anything_reads_what_it_wrote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``check-generated-client.mjs`` regenerates ``src/api/generated`` in place, and
        ``build`` writes the bundle Playwright serves."""
        started = self.record_runs(monkeypatch)

        assert check.main(["--concurrent"]) == 0
        api = self.first_index(started, "check:api-client")
        assert api < self.first_index(started, "run test")
        assert api < self.first_index(started, "run build")
        assert self.first_index(started, "run build") < self.first_index(started, "test:e2e")

    def test_a_failure_is_still_a_failure_and_names_its_stage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.record_runs(monkeypatch, fails="-m mypy")

        assert check.main(["--concurrent"]) == 1
        assert "Failed at stage 'mypy'" in capsys.readouterr().err

    def test_a_failed_stage_grounds_what_had_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``api`` fails, so the three stages that wait on it never start; nothing that
        was already running is killed."""
        started = self.record_runs(monkeypatch, fails="export_openapi.py")

        assert check.main(["--concurrent"]) == 1
        joined = [" ".join(command) for command in started]
        assert not any("test:e2e" in command for command in joined)
        assert not any("run build" in command for command in joined)

    def test_a_concurrent_run_reports_wall_clock_beside_the_sum(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Under concurrency the sum of the stages is what the gate *would* have cost
        serially, which is worth keeping -- but it is not what it cost."""
        self.record_runs(monkeypatch)

        check.main(["--concurrent"])

        printed = capsys.readouterr().out
        assert re.search(r"^  wall +\d+\.\d+s", printed, re.MULTILINE)

    def test_a_serial_run_reports_no_wall_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There it would be the total under another name."""
        TestTheUnqualifiedGate.record_runs(monkeypatch)

        check.main([])

        assert "wall" not in capsys.readouterr().out

    def test_pytest_reserves_cores_for_the_lane_beside_it(self) -> None:
        """``gate_slots`` divides the machine between *gates*, and the frontend lane of a
        concurrent run is not a gate -- it is inside one."""
        stage = next(stage for stage in check.stages() if stage.name == "pytest")

        alone, _ = check.commands_for(stage, "npm.cmd")
        reserved, _ = check.commands_for(stage, "npm.cmd", reserve=check.CONCURRENT_RESERVE)

        assert alone[0][alone[0].index("-n") + 1] == "auto"
        assert reserved[0][reserved[0].index("-n") + 1] != "auto"

    def test_captured_output_cannot_kill_the_run_it_is_reporting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first real concurrent gate on this machine passed all ten stages and then
        died in ``print``: Black's emoji, re-encoded to a redirected cp1252 stdout."""

        class Cp1252Stdout:
            encoding = "cp1252"

        monkeypatch.setattr(check.sys, "stdout", Cp1252Stdout())

        assert check.printable("all done \u2728 \ufffd") == "all done ? ?"

    def test_printable_leaves_text_the_terminal_can_take(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Utf8Stdout:
            encoding = "utf-8"

        monkeypatch.setattr(check.sys, "stdout", Utf8Stdout())

        assert check.printable("all done \u2728") == "all done \u2728"
