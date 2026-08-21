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
from pathlib import Path
from types import ModuleType

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
        """``check:api`` is one stage precisely because its two halves are ordered."""
        api = next(stage for stage in check.stages() if stage.name == "api")
        assert api.args[-1] == "check:api"

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
        assert len(commands) == len(check.stages())

    def test_a_selection_runs_only_what_was_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands = self.record_runs(monkeypatch)

        assert check.main(["--only", "black"]) == 0
        assert len(commands) == 1

    def test_listing_the_stages_runs_none_of_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        commands = self.record_runs(monkeypatch)

        assert check.main(["--list"]) == 0
        assert commands == []

    def test_the_frontend_stages_are_exactly_the_frontend_gate(self) -> None:
        """Two descriptions of one order drift apart, so assert they agree.

        ``npm run check`` is documented as the frontend gate and is run on its own.
        Since task-189 the stage table is the authority on order, but a stage added to
        one and not the other is a check that silently stops running in the gate that
        matters.
        """
        manifest = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        scripted = re.findall(r"npm run ([\w:-]+)", manifest["scripts"]["check"])
        from_table = [stage.args[-1] for stage in check.stages() if stage.cwd == ROOT / "frontend"]

        assert sorted(scripted) == sorted(from_table)


# --- the pytest stage's two options -------------------------------------------------


class TestPytestOptions:
    """Coverage and parallelism, both moved out of ``addopts`` by task-233.

    The suite is 89% of the gate. Serial with coverage it was 540s on this machine;
    serial without, 343s; at ``-n auto``, 43s -- same commit, same 2538 passing. The
    numbers are in ENGINEERING.md; what is guarded here is that the flags still mean
    what those numbers were measured under.
    """

    @staticmethod
    def pytest_args(**options: bool) -> list[str]:
        stage = next(s for s in check.stages(**options) if s.name == "pytest")
        return [str(arg) for arg in stage.args]

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
        plain = {s.name: s.args for s in check.stages()}
        loud = {s.name: s.args for s in check.stages(coverage=True, parallel=False)}

        assert [name for name in plain if plain[name] != loud[name]] == ["pytest"]

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
        assert len(commands) == len(check.stages())
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

        kinds = [record["kind"] for record in read_phases(directory)]
        assert kinds == ["gate_started", "gate_finished"]

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

        assert len(commands) == len(check.stages())
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

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            # Everything after the runner, which is the only part a stage chooses.
            if command[1:] == list(wanted.args[1:]):
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

    def test_every_stage_is_named(self) -> None:
        """A stage the handbook does not mention is one nobody can ask for by name."""
        text = self.handbook()
        for stage in check.stages():
            assert f"`{stage.name}`" in text, f"ENGINEERING.md never names {stage.name}"

    def test_the_selection_flags_are_documented_as_not_being_the_gate(self) -> None:
        text = self.handbook()
        assert "--from" in text and "--only" in text
        assert "PARTIAL RUN" in text

    def test_the_gate_before_commit_contradiction_stays_resolved(self) -> None:
        """task-189 decided which side wins. Losing the sentence loses the decision."""
        text = self.handbook()
        assert "The gate runs before the commit" in text
        assert "regenerate, run\n    the gate, then commit" in text.replace("**", "")
