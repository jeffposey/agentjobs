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
