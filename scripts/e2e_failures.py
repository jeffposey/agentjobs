"""Tell a browser that died between two tests apart from a test that failed (task-404).

**What this is for.** The `e2e` stage runs each of its workers against one Chromium
process -- no spec overrides a worker-scoped option, so a worker starts one browser and
keeps it -- and on this machine such a process is sometimes terminated between two
tests. Observed on at least thirteen gate runs
from 2026-08-23 to 2026-09-13, always the same shape: the previous test passed, the next
one fails with `browser.newContext: Target page, context or browser has been closed`
before a line of it runs, and Playwright starts a fresh worker for the test after that.
The stage said only that a named test failed, so every reader re-ran the gate to find out
it was the machine -- and a scripted finish cannot re-run, so it handed a green branch to a
person instead.

**What kills the process was not found**, and what was ruled out is on task-404's record
with the evidence: it is not a native crash (WER logs those here and has none for
Chromium), not a console control event, not memory, not this repository's test suite.

**The rule is narrow on purpose**, because a retry that hides a browser the application
crashed is worse than the flake. A failure is a *browser death* only when every error on
it is Playwright's own `context` fixture failing to open a context on a browser that is
already gone. Such an error has no source location -- it is raised before the test body
starts, so no line of the spec is on it. A browser that dies *while a test holds a page*
fails at `page.something` with a location in the spec, and stays a real failure: that is
the case an application could have caused. Reproduced both ways with a spec that
terminates its own worker's browser from outside; see the task record.

Pure functions over Playwright's JSON report, so the rule is tested without a browser.
`check.py` decides what to do with the answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPORT = "playwright-report/e2e-results.json"
"""Where `frontend/playwright.config.ts` has the JSON reporter write, relative to
`frontend/`. The two are asserted to agree (`tests/test_e2e_failures.py`).

Not under `test-results/`: Playwright empties that directory at the start of every run,
and the retry is a run.
"""

BROWSER_GONE = "browser.newContext: Target page, context or browser has been closed"
"""The one message this module recognises, exactly as Playwright 1.62 words it."""

MAX_RETRIED = 2
"""The most browser deaths one run may have and still be retried.

Every sighting so far cost exactly one test, which is what a single termination costs
(the next test gets a new worker and a new browser). A run that loses its browser over and
over is not a flake but a condition -- a broken Chromium install, a machine out of
something -- and a retry would hide exactly the thing worth reading.
"""

ANSI = re.compile("\x1b\\[[0-9;]*m")


@dataclass(frozen=True)
class Failure:
    """One test the run reported as failing."""

    title: str
    browser_gone: bool
    message: str


@dataclass(frozen=True)
class Outcome:
    """What a Playwright run's JSON report says about its failures."""

    failures: tuple[Failure, ...]
    passed: int
    run_errors: int
    """Errors outside any test, such as the web server failing to start. Never retried."""

    @property
    def browser_deaths(self) -> tuple[Failure, ...]:
        return tuple(failure for failure in self.failures if failure.browser_gone)

    @property
    def real_failures(self) -> tuple[Failure, ...]:
        return tuple(failure for failure in self.failures if not failure.browser_gone)

    @property
    def retryable(self) -> bool:
        """Every failure is a browser death, and there are few enough to be a flake."""
        return (
            self.run_errors == 0
            and bool(self.failures)
            and not self.real_failures
            and len(self.failures) <= MAX_RETRIED
        )


def plain(text: object) -> str:
    """Playwright colours its messages even in a JSON report."""
    return ANSI.sub("", str(text))


def is_browser_death(result: dict[str, Any]) -> bool:
    """Did this result fail only because the browser was gone before the test began?"""
    errors = result.get("errors") or []
    if not errors:
        return False
    for error in errors:
        if error.get("location"):
            # A line of the spec is on it, so the test body was running. Real.
            return False
        message = plain(error.get("message", ""))
        message = message.removeprefix("Error: ")
        if not message.startswith(BROWSER_GONE):
            return False
    return True


def _walk(suite: dict[str, Any], trail: tuple[str, ...]) -> Any:
    for spec in suite.get("specs") or []:
        yield spec, trail
    for child in suite.get("suites") or []:
        yield from _walk(child, (*trail, str(child.get("title", ""))))


def parse(report: dict[str, Any]) -> Outcome:
    """Classify every failing test in a report. Raises nothing for a report it can read."""
    failures: list[Failure] = []
    passed = 0
    for top in report.get("suites") or []:
        # The top-level suite is the file; its title adds nothing the location does not.
        for spec, trail in _walk(top, ()):
            for test in spec.get("tests") or []:
                status = test.get("status")
                if status in ("expected", "flaky"):
                    passed += 1
                    continue
                if status != "unexpected":
                    continue
                results = test.get("results") or [{}]
                last = results[-1]
                errors = last.get("errors") or []
                first = plain(errors[0].get("message", "")) if errors else ""
                where = f"e2e/{spec.get('file', '?')}:{spec.get('line', '?')}"
                title = " › ".join([where, *[t for t in trail if t], str(spec.get("title", ""))])
                failures.append(
                    Failure(
                        title=title,
                        browser_gone=is_browser_death(last),
                        message=first.removeprefix("Error: ").split("\n", 1)[0],
                    )
                )
    return Outcome(
        failures=tuple(failures),
        passed=passed,
        run_errors=len(report.get("errors") or []),
    )


def read(path: Path) -> Outcome | None:
    """The report at `path`, or None when there is none to read.

    None is never a reason to retry: `check.py` deletes the report before the stage runs,
    so a missing or unreadable one means Playwright did not get as far as reporting, and
    that failure is not one this module can vouch for.
    """
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    return parse(report)


def _tests(count: int) -> str:
    return "1 test" if count == 1 else f"{count} tests"


def explain(outcome: Outcome) -> str | None:
    """What to print after a red run, or None when no browser died.

    The point of task-404's first criterion: a reader of the stage output can tell a
    browser that was already gone from a test that failed, without opening a trace.
    """
    deaths = outcome.browser_deaths
    if not deaths:
        return None
    lines = [
        f"BROWSER GONE, NOT A TEST FAILURE: {_tests(len(deaths))} never started.",
        "Chromium was terminated between two tests, so Playwright's own fixture had no",
        f"browser to open a page in (`{BROWSER_GONE}`,",
        "raised before the first line of the test). That is the machine, not the code under",
        "test -- see task-404.",
        *[f"  - {failure.title}" for failure in deaths],
    ]
    real = outcome.real_failures
    if real:
        lines += [
            "",
            f"The other {_tests(len(real))} failed for real, so nothing is retried:",
            *[f"  - {failure.title}: {failure.message}" for failure in real],
        ]
    elif outcome.run_errors:
        lines += ["", "The run also reported errors outside any test, so nothing is retried."]
    elif len(deaths) > MAX_RETRIED:
        lines += [
            "",
            f"More than {MAX_RETRIED} in one run is not a flake, so nothing is retried: "
            "something is killing the browser repeatedly, and that is worth reading.",
        ]
    else:
        lines += [
            "",
            f"Re-running only {'that test' if len(deaths) == 1 else 'those tests'}, once.",
        ]
    return "\n".join(lines)


def judge_retry(asked: int, retry: Outcome | None, exit_code: int) -> tuple[bool, str]:
    """Whether a retry of `asked` browser deaths is a pass, and the sentence that says so.

    It has to have run exactly those tests and passed every one. A retry that ran nothing
    -- `--last-failed` with no record of the last run -- exits 0 and proves nothing.
    """
    if retry is None:
        return False, "The retry wrote no report, so it proves nothing; `e2e` stays failed."
    if exit_code != 0 or retry.failures or retry.run_errors:
        return False, (
            "The retry failed too, so this is a real failure and `e2e` stays failed. "
            "Read the retry's output above, not the first run's."
        )
    if retry.passed != asked:
        return False, (
            f"The retry ran {_tests(retry.passed)}, not the {_tests(asked)} it was asked to "
            "re-run, so it proves nothing; `e2e` stays failed."
        )
    return True, (
        f"RETRIED AND PASSED: the {_tests(asked)} whose browser was gone passed on a second "
        "run, so `e2e` counts as passed. The browser death above is what happened on the "
        "first run; it is not evidence about the code under test."
    )
