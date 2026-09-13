"""A browser that died between tests is told apart from a failing test, and only it is retried.

Task-404. The shapes below are Playwright 1.62's JSON report as captured on 2026-09-13 from
a spec that terminated its own worker's `chrome-headless-shell.exe` from outside: once while
no test held a page (the observed flake), once in the middle of a test body (a death the
application could have caused). Colour codes are kept because Playwright writes them into
the JSON too.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


e2e_failures = load_script("e2e_failures")
check = load_script("check")

GONE = (
    "Error: browser.newContext: Target page, context or browser has been closed\n"
    "Browser logs:\n\n<launching> C:\\...\\chrome-headless-shell.exe --disable-field-trial-config\n"
    "<launched> pid=48816"
)
MID_BODY = (
    "Error: page.setContent: Target page, context or browser has been closed\n\n"
    "\x1b[0m \x1b[90m 38 |\x1b[39m   \x1b[36mawait\x1b[39m page.setContent(...)"
)
ASSERTION = "Error: \x1b[2mexpect(\x1b[22m\x1b[31mlocator\x1b[39m\x1b[2m).\x1b[22mtoHaveText failed"
SPEC = "C:\\projects\\worktrees\\agentjobs-404\\frontend\\e2e\\task-tree.spec.ts"


def gone() -> dict[str, Any]:
    """Raised inside Playwright's own `context` fixture: no line of the spec is on it."""
    return {"message": GONE}


def located(message: str, line: int = 40) -> dict[str, Any]:
    return {"message": message, "location": {"file": SPEC, "line": line, "column": 14}}


def entry(title: str, line: int, *errors: dict[str, Any], status: str | None = None) -> dict:
    """One spec entry as the JSON reporter nests it."""
    return {
        "title": title,
        "file": "task-tree.spec.ts",
        "line": line,
        "tests": [
            {
                "status": status or ("unexpected" if errors else "expected"),
                "results": [{"status": "failed" if errors else "passed", "errors": list(errors)}],
            }
        ],
    }


def report(*specs: dict, errors: list[dict] | None = None) -> dict[str, Any]:
    return {
        "suites": [
            {
                "title": "task-tree.spec.ts",
                "specs": [],
                "suites": [{"title": "the task list as a sidebar tree", "specs": list(specs)}],
            }
        ],
        "errors": errors or [],
    }


# --- the rule ---------------------------------------------------------------------


class TestWhatCountsAsABrowserDeath:
    def test_the_observed_flake_does(self) -> None:
        assert e2e_failures.is_browser_death({"errors": [gone()]})

    def test_a_browser_lost_while_the_test_held_a_page_does_not(self) -> None:
        """The case a retry must never hide: the body was running, so the app is a suspect."""
        assert not e2e_failures.is_browser_death({"errors": [located(MID_BODY)]})

    def test_the_same_message_with_a_location_does_not(self) -> None:
        """A spec that calls `browser.newContext()` itself is test code, not the fixture."""
        message = "Error: browser.newContext: Target page, context or browser has been closed"
        assert not e2e_failures.is_browser_death({"errors": [located(message)]})

    def test_an_assertion_does_not(self) -> None:
        assert not e2e_failures.is_browser_death({"errors": [located(ASSERTION)]})

    def test_a_timeout_with_no_location_does_not(self) -> None:
        """Locationless is necessary, not sufficient."""
        assert not e2e_failures.is_browser_death(
            {"errors": [{"message": "Test timeout of 30000ms exceeded."}]}
        )

    def test_one_real_error_beside_it_spoils_it(self) -> None:
        assert not e2e_failures.is_browser_death({"errors": [gone(), located(ASSERTION)]})

    def test_a_result_with_no_errors_is_not_one(self) -> None:
        assert not e2e_failures.is_browser_death({"errors": []})

    def test_colour_codes_do_not_hide_the_message(self) -> None:
        coloured = "\x1b[31m" + GONE.replace(
            "browser.newContext", "\x1b[2mbrowser.newContext\x1b[22m"
        )
        assert e2e_failures.is_browser_death({"errors": [{"message": coloured}]})


class TestParsingAReport:
    def test_failures_are_named_the_way_the_line_reporter_names_them(self) -> None:
        outcome = e2e_failures.parse(report(entry("Up and Down move the selection", 245, gone())))

        assert [f.title for f in outcome.failures] == [
            "e2e/task-tree.spec.ts:245 › the task list as a sidebar tree › "
            "Up and Down move the selection"
        ]

    def test_passes_are_counted_and_failures_classified(self) -> None:
        outcome = e2e_failures.parse(
            report(
                entry("a", 10),
                entry("b", 20, gone()),
                entry("d", 30, located(ASSERTION)),
                entry("flaky", 40, status="flaky"),
            )
        )

        assert outcome.passed == 2
        assert [f.title.rsplit(" › ", 1)[1] for f in outcome.browser_deaths] == ["b"]
        assert [f.message for f in outcome.real_failures] == ["expect(locator).toHaveText failed"]

    def test_skipped_tests_are_neither(self) -> None:
        outcome = e2e_failures.parse(report(entry("s", 10, status="skipped")))

        assert outcome.passed == 0 and not outcome.failures


class TestWhatIsRetried:
    def test_a_single_browser_death_is(self) -> None:
        assert e2e_failures.parse(report(entry("b", 20, gone()))).retryable

    def test_two_are(self) -> None:
        assert e2e_failures.parse(report(entry("b", 20, gone()), entry("c", 30, gone()))).retryable

    def test_more_than_that_is_a_condition_not_a_flake(self) -> None:
        specs = [entry(name, line, gone()) for name, line in (("b", 1), ("c", 2), ("e", 3))]

        assert not e2e_failures.parse(report(*specs)).retryable

    def test_nothing_is_retried_beside_a_real_failure(self) -> None:
        """The run is red on its merits whatever else happened in it."""
        outcome = e2e_failures.parse(
            report(entry("b", 20, gone()), entry("d", 30, located(ASSERTION)))
        )

        assert not outcome.retryable

    def test_nothing_is_retried_when_the_run_itself_errored(self) -> None:
        outcome = e2e_failures.parse(
            report(entry("b", 20, gone()), errors=[{"message": "Error: webServer exited"}])
        )

        assert not outcome.retryable

    def test_a_run_with_no_failures_has_nothing_to_retry(self) -> None:
        assert not e2e_failures.parse(report(entry("a", 10))).retryable


class TestWhatTheReaderIsTold:
    def test_nothing_extra_when_no_browser_died(self) -> None:
        assert (
            e2e_failures.explain(e2e_failures.parse(report(entry("d", 30, located(ASSERTION)))))
            is None
        )

    def test_a_death_is_named_as_not_a_test_failure(self) -> None:
        text = e2e_failures.explain(e2e_failures.parse(report(entry("b", 245, gone()))))

        assert text is not None
        assert text.startswith("BROWSER GONE, NOT A TEST FAILURE: 1 test never started.")
        assert "e2e/task-tree.spec.ts:245" in text
        assert "Re-running only that test, once." in text

    def test_a_mixed_run_says_which_is_which_and_retries_nothing(self) -> None:
        text = e2e_failures.explain(
            e2e_failures.parse(report(entry("b", 20, gone()), entry("d", 30, located(ASSERTION))))
        )

        assert text is not None
        assert "The other 1 test failed for real, so nothing is retried:" in text
        assert "d: expect(locator).toHaveText failed" in text
        assert "Re-running" not in text

    def test_too_many_says_why_it_is_not_retried(self) -> None:
        specs = [entry(name, line, gone()) for name, line in (("b", 1), ("c", 2), ("e", 3))]
        text = e2e_failures.explain(e2e_failures.parse(report(*specs)))

        assert text is not None and "is not a flake, so nothing is retried" in text


class TestJudgingTheRetry:
    def passing(self, count: int) -> Any:
        return e2e_failures.parse(report(*[entry(str(i), i) for i in range(count)]))

    def test_exactly_those_tests_passing_is_a_pass(self) -> None:
        passed, text = e2e_failures.judge_retry(1, self.passing(1), 0)

        assert passed and text.startswith("RETRIED AND PASSED")

    def test_a_retry_that_ran_nothing_proves_nothing(self) -> None:
        """`--last-failed` with no record of the last run exits 0 having run no test."""
        passed, text = e2e_failures.judge_retry(1, self.passing(0), 0)

        assert not passed and "ran 0 tests, not the 1 test" in text

    def test_a_red_retry_is_a_real_failure(self) -> None:
        passed, _ = e2e_failures.judge_retry(
            1, e2e_failures.parse(report(entry("b", 1, gone()))), 1
        )

        assert not passed

    def test_no_report_is_no_evidence(self) -> None:
        assert not e2e_failures.judge_retry(1, None, 0)[0]


class TestReadingTheFile:
    def test_a_missing_report_is_none(self, tmp_path: Path) -> None:
        assert e2e_failures.read(tmp_path / "absent.json") is None

    def test_a_torn_report_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "torn.json"
        path.write_text('{"suites": [', encoding="utf-8")

        assert e2e_failures.read(path) is None

    def test_the_config_writes_where_the_gate_reads(self) -> None:
        config = (ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")

        assert f'outputFile: "{e2e_failures.REPORT}"' in config

    def test_the_config_still_asks_playwright_for_no_retries_of_its_own(self) -> None:
        """The narrow retry is the gate's; a blanket one would re-run a crash the app caused."""
        config = (ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")

        assert "retries: 0," in config


# --- the gate ---------------------------------------------------------------------


class TestTheGateRetriesOnlyThat:
    """`check.py --only e2e`, with Playwright replaced by a script of what it wrote.

    `--only`, so no receipt can be issued by a simulated run.
    """

    @pytest.fixture(autouse=True)
    def isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentjobs.dispatch.phases import RUN_DIR_ENV

        monkeypatch.delenv(RUN_DIR_ENV, raising=False)
        monkeypatch.setattr(check, "E2E_REPORT", tmp_path / "e2e-results.json")
        monkeypatch.setattr(check, "setup_problems", lambda root, origin: [])
        monkeypatch.setattr(check.shutil, "which", lambda name: "npm.cmd")

    def playwright(
        self, monkeypatch: pytest.MonkeyPatch, runs: list[tuple[int, dict | None]]
    ) -> list[list[str]]:
        """Each e2e invocation pops the next (exit code, report written) pair."""
        seen: list[list[str]] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "test:e2e" not in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            seen.append(list(command))
            code, written = runs.pop(0)
            if written is not None:
                check.E2E_REPORT.write_text(json.dumps(written), encoding="utf-8")
            if code and kwargs.get("check"):
                raise subprocess.CalledProcessError(code, command)
            return subprocess.CompletedProcess(command, code, "", "")

        monkeypatch.setattr(check.subprocess, "run", run)
        return seen

    @pytest.mark.parametrize("flags", [[], ["--concurrent"]])
    def test_a_browser_death_is_explained_retried_once_and_passes(
        self, flags: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = self.playwright(
            monkeypatch,
            [(1, report(entry("a", 10), entry("b", 245, gone()))), (0, report(entry("b", 245)))],
        )

        assert check.main(["--only", "e2e", *flags]) == 0

        out = capsys.readouterr().out
        assert "BROWSER GONE, NOT A TEST FAILURE: 1 test never started." in out
        assert "RETRIED AND PASSED" in out
        assert seen[1][-2:] == ["--", "--last-failed"]
        assert len(seen) == 2

    @pytest.mark.parametrize("flags", [[], ["--concurrent"]])
    def test_a_real_failure_is_never_retried(
        self, flags: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = self.playwright(
            monkeypatch, [(1, report(entry("b", 20, gone()), entry("d", 30, located(ASSERTION))))]
        )

        assert check.main(["--only", "e2e", *flags]) == 1

        assert len(seen) == 1
        assert "failed for real, so nothing is retried" in capsys.readouterr().out

    def test_a_red_retry_leaves_the_stage_failed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.playwright(
            monkeypatch,
            [(1, report(entry("b", 20, gone()))), (1, report(entry("b", 20, located(ASSERTION))))],
        )

        assert check.main(["--only", "e2e"]) == 1
        assert "The retry failed too" in capsys.readouterr().out

    def test_a_plain_failure_prints_nothing_new(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = self.playwright(monkeypatch, [(1, report(entry("d", 30, located(ASSERTION))))])

        assert check.main(["--only", "e2e"]) == 1
        assert len(seen) == 1
        assert "BROWSER GONE" not in capsys.readouterr().out

    def test_a_stale_report_from_an_earlier_run_is_never_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Playwright died before reporting; the file on disk is the previous run's."""
        check.E2E_REPORT.write_text(json.dumps(report(entry("b", 20, gone()))), encoding="utf-8")
        seen = self.playwright(monkeypatch, [(1, None)])

        assert check.main(["--only", "e2e"]) == 1
        assert len(seen) == 1
