"""The cheap half of the front-end budget: that it still exists and still has teeth.

`frontend/e2e/perf-budget.spec.ts` is the measurement, and it runs in the `e2e` stage
where it belongs. This file guards the two ways a performance budget dies, neither of
which the measurement itself can notice:

- **It is deleted**, or moved somewhere the `e2e` stage does not run it.
- **It is loosened until it catches nothing.** That is the ordinary end of a wall-clock
  assertion: it fails once on a busy machine, somebody adds a zero, and the number that
  remains is not a budget. So the ceiling is asserted here, where raising it is a diff a
  reviewer reads rather than a constant nobody looks at twice.

The bound is task-487's own sentence and not a round number: task-135 took this
interaction to 263ms, and the budget exists to catch that becoming 2.6 seconds. A
threshold above 2,600ms would not, whatever else it caught. Raising it past that is a
decision about what this repository no longer guards -- make it deliberately, say so in
the constant's comment, and change the number here in the same commit.

These are text assertions about a TypeScript file, which is a blunt instrument and
deliberately so: the alternative is a Node process inside the Python suite to learn one
integer, which costs more than it tells anybody.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Where the budget has to live: the `e2e` stage runs `frontend/e2e/*.spec.ts` and
#: nothing else, so a spec outside this directory is a spec the gate never runs.
SPEC = ROOT / "frontend" / "e2e" / "perf-budget.spec.ts"

#: Above this, the budget stops catching the regression it was written for. See the
#: module docstring: 263ms was the measured figure, and ten times it is the thing to see.
CEILING_MS = 2_600


@pytest.fixture(scope="module")
def spec() -> str:
    assert SPEC.exists(), (
        f"{SPEC.relative_to(ROOT)} is gone. It is the only thing in scripts/check.py "
        "that measures the browser; deleting it returns the front end to being "
        "unguarded, which is the state task-487 was filed about."
    )
    return SPEC.read_text(encoding="utf-8")


def _constant(spec: str, name: str) -> int:
    """The integer a `const NAME = 1_234;` line declares."""
    found = re.search(rf"^const {name} = ([\d_]+);$", spec, re.MULTILINE)
    assert found is not None, f"{name} is no longer declared as a plain constant in {SPEC.name}"
    return int(found.group(1).replace("_", ""))


def test_the_click_to_rendered_budget_still_catches_a_tenfold_regression(spec: str) -> None:
    measured = _constant(spec, "CLICK_TO_RENDERED_MS")
    assert measured <= CEILING_MS, (
        f"CLICK_TO_RENDERED_MS is {measured}ms, above the {CEILING_MS}ms ceiling. "
        "task-135 took this interaction to 263ms and the budget is there to catch that "
        "becoming 2.6 seconds; a threshold above the ceiling cannot. If the budget has "
        "to be raised, raise this ceiling in the same commit and say in the constant's "
        "comment what the new number is derived from."
    )


def test_the_budget_says_it_is_an_order_of_magnitude_check(spec: str) -> None:
    """ac-3, kept honest.

    The warning is the half of the constant that stops a later reader tuning it towards
    the measured value -- which is how a performance test becomes a flaky one and then
    a deleted one. It is prose, so nothing but a test asserting it keeps it there.
    """
    assert "order-of-magnitude" in spec
    assert "must not be tightened" in spec


def test_the_corpus_the_budget_runs_against_is_stated(spec: str) -> None:
    """A figure measured against an unstated corpus says nothing (ac-4).

    Both halves: the size, and the spec's own judgement that it is enough for a
    per-interaction collapse and not enough for a per-record one.
    """
    assert _constant(spec, "CORPUS_SIZE") > 0
    assert "not enough for a per-row one" in spec
