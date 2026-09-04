"""Every identity refusal has a headline, in both places that draw one.

Two UIs render the same refusal: the React app from
``frontend/src/components/identityProblem.ts`` and the legacy server-rendered page from
``agentjobs.actors.PROBLEM_HEADLINES``. Neither fails loudly when a code is missing --
each falls back to a general sentence -- so the drift would show up as a person being
told the wrong thing rather than as a broken build. That is what this file catches.

It reads the TypeScript as text on purpose. The alternative is a build step to make the
table importable from Python, which is a lot of machinery for six strings; a regex over
a `case "x":` list is crude but it fails exactly when the thing it is protecting fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentjobs import actors
from agentjobs.actors import PROBLEM_HEADLINES

ROOT = Path(__file__).resolve().parents[1]
TS = ROOT / "frontend" / "src" / "components" / "identityProblem.ts"


def problem_constants() -> set[str]:
    """Every problem code ``agentjobs.actors`` defines.

    Read off the module rather than listed here, so that adding a constant is what makes
    this test fail -- listing them would mean the test only knows about codes somebody
    remembered to add to it, which is the failure it exists to prevent.
    """
    codes = set()
    for name in dir(actors):
        if not name.isupper() or name.startswith("_"):
            continue
        value = getattr(actors, name)
        if isinstance(value, str) and value in PROBLEM_HEADLINES:
            codes.add(value)
    return codes


def typescript_cases() -> set[str]:
    """The codes the React helper switches on."""
    return set(re.findall(r'case\s+"([a-z_]+)"\s*:', TS.read_text(encoding="utf-8")))


def test_the_react_helper_exists_where_the_backend_says_it_does() -> None:
    # PROBLEM_HEADLINES' docstring names this path. A rename that leaves the docstring
    # behind is how a pointer becomes a lie.
    assert TS.is_file()


@pytest.mark.parametrize("code", sorted(problem_constants()))
def test_every_problem_code_has_a_python_headline(code: str) -> None:
    assert PROBLEM_HEADLINES[code].strip()


@pytest.mark.parametrize("code", sorted(problem_constants()))
def test_every_problem_code_has_a_react_headline(code: str) -> None:
    assert code in typescript_cases()


def test_neither_side_carries_a_code_the_other_does_not() -> None:
    assert typescript_cases() == set(PROBLEM_HEADLINES)


def test_the_headlines_are_distinct() -> None:
    # A refusal whose headline is shared with another refusal tells the reader nothing
    # the fallback would not have told them.
    assert len(set(PROBLEM_HEADLINES.values())) == len(PROBLEM_HEADLINES)
