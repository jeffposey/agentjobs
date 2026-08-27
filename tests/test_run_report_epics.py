"""The epic timeline arithmetic behind any before/after claim about walk concurrency.

``ENGINEERING.md`` says a cycle-time claim is a before/after or it is an anecdote, and
names ``scripts/run_report.py`` as where the pair comes from. Until task-223 that tool
had no concept of an epic at all: no parent grouping, no cross-run overlap, and -- the
one that mattered -- **no measure of the gap between one child closing and the next
starting**, which is exactly the quantity a concurrent walk removes.

So the arithmetic gets its own tests rather than being trusted because the table looked
plausible. Every case here is a hand-built timeline whose answer can be checked by
counting on fingers, because the failure mode of a measurement tool is not a crash; it is
a number that is quietly wrong and gets quoted for a month.

The three quantities and why they are three:

* **wall** -- time inside a sitting, which is what somebody watching the epic waited.
* **idle** -- wall with no child running. The scheduler owns this, and only this.
* **paused** -- time between sittings: overnight, a review nobody got to, the epic simply
  not being driven. Reported and never folded into idle, because attributing a person's
  week to the walk is how the first cut of this read 92%.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load() -> Any:
    """Import ``scripts/run_report.py``, which is a script rather than a package module."""
    spec = importlib.util.spec_from_file_location(
        "run_report_under_test", REPO_ROOT / "scripts" / "run_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_report = _load()

START = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def span(module: Any, task: str, offset_minutes: float, length_minutes: float) -> Any:
    return module.Span(
        task_id=task,
        label=f"run_{task}",
        started_at=START + timedelta(minutes=offset_minutes),
        finished_at=START + timedelta(minutes=offset_minutes + length_minutes),
    )


def epic(spans: List[Any], **kwargs: Any) -> Any:
    return run_report.Epic(parent_id="task-000", spans=spans, **kwargs)


class TestSerialEpics:
    """The shape every epic in this machine's ledger had before task-223."""

    def test_the_gap_between_children_is_idle(self) -> None:
        one = span(run_report, "task-a", 0, 30)
        two = span(run_report, "task-b", 40, 20)
        measured = epic([one, two])
        assert measured.wall_seconds == 60 * 60
        assert measured.busy_seconds == 50 * 60
        assert measured.idle_seconds == 10 * 60
        assert measured.peak_concurrency == 1

    def test_parallelism_of_a_serial_epic_is_below_one(self) -> None:
        """``work / wall``: 1.00 would mean no gaps at all, and gaps are the point."""
        measured = epic([span(run_report, "task-a", 0, 30), span(run_report, "task-b", 40, 20)])
        assert measured.work_seconds == 50 * 60
        assert measured.parallelism == 50 / 60


class TestConcurrentEpics:
    def test_overlapping_children_are_counted_once_in_busy_and_twice_in_work(self) -> None:
        """The one place a sum is right, and why both figures are printed.

        ``busy`` is a union because a percentage over a hundred is not a share of a
        timeline. ``work`` is a sum because it is the only figure that shows the saving:
        running two children at once reduces the wall clock and the union together, so a
        report of unions alone would hide the improvement entirely.
        """
        measured = epic([span(run_report, "task-a", 0, 30), span(run_report, "task-b", 10, 30)])
        assert measured.wall_seconds == 40 * 60
        assert measured.busy_seconds == 40 * 60
        assert measured.work_seconds == 60 * 60
        assert measured.idle_seconds == 0
        assert measured.parallelism == 1.5
        assert measured.peak_concurrency == 2

    def test_peak_concurrency_counts_the_deepest_moment_not_the_average(self) -> None:
        measured = epic(
            [
                span(run_report, "task-a", 0, 60),
                span(run_report, "task-b", 10, 5),
                span(run_report, "task-c", 12, 5),
            ]
        )
        assert measured.peak_concurrency == 3

    def test_children_that_touch_end_to_end_are_not_concurrent(self) -> None:
        """A child starting the instant another ends is a fast handover, not an overlap."""
        measured = epic([span(run_report, "task-a", 0, 30), span(run_report, "task-b", 30, 30)])
        assert measured.peak_concurrency == 1
        assert measured.idle_seconds == 0


class TestSittings:
    """Overnight is not idle, and the distinction is the whole honesty of the table."""

    def test_a_long_gap_ends_a_sitting_instead_of_counting_as_idle(self) -> None:
        measured = epic(
            [span(run_report, "task-a", 0, 30), span(run_report, "task-b", 60 * 14, 30)]
        )
        assert len(measured.sittings) == 2
        assert measured.wall_seconds == 60 * 60
        assert measured.idle_seconds == 0
        assert measured.paused_seconds == pytest_approx(13.5 * 3600)

    def test_a_short_gap_stays_inside_one_sitting(self) -> None:
        measured = epic([span(run_report, "task-a", 0, 30), span(run_report, "task-b", 45, 30)])
        assert len(measured.sittings) == 1
        assert measured.idle_seconds == 15 * 60
        assert measured.paused_seconds == 0

    def test_the_ceiling_is_a_parameter_rather_than_a_hidden_judgement(self) -> None:
        spans = [span(run_report, "task-a", 0, 30), span(run_report, "task-b", 120, 30)]
        assert len(epic(spans).sittings) == 2
        assert len(epic(spans, gap_ceiling_seconds=3 * 3600).sittings) == 1


class TestTheTable:
    def test_it_says_so_when_no_epic_could_be_reconstructed(self) -> None:
        """Silence would read as "no idle", which is the one answer it must not imply."""
        rendered = run_report.epic_report([])
        assert "No epic could be reconstructed" in rendered

    def test_a_serial_epic_renders_as_serial(self) -> None:
        rendered = run_report.epic_report(
            [epic([span(run_report, "task-a", 0, 30), span(run_report, "task-b", 40, 20)])]
        )
        assert "ran serially          1 of 1" in rendered
        assert "1.00 is serial" in rendered


def pytest_approx(value: float) -> Any:
    import pytest

    return pytest.approx(value)
