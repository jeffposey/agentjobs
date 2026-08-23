"""Every REST operation must appear in `docs/api-reference.md`.

The Big Dawg Audit found the prose documenting 74 of 94 operations while every
*structural* check passed: `openapi.json` matched the routes exactly and the generated
TypeScript client matched `openapi.json`. Nothing compared either against the page a
human reads, so the page fell behind by a whole subsystem -- the dispatch family, the
review actions, `/api/version` -- without a single check going red.

This is the cheap version of that comparison. It does not read the prose; it only
insists that a route which exists is *mentioned*. That is a low bar deliberately: a bar
high enough to argue about is a bar someone turns off.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentjobs.api.main import app

DOC = Path(__file__).resolve().parents[1] / "docs" / "api-reference.md"

PARAM = re.compile(r"\{[^}]+\}")
SCOPE_PREFIX = "/api/projects/{}"


def _normalise(text: str) -> str:
    """Erase path-parameter *names* so `{task_id}` and `{id}` compare equal.

    The document and the routes disagree about what to call a path parameter, and they
    are both entitled to: `{id}` reads better in a table and `{task_id}` is clearer in
    code. Neither spelling is a documentation defect, so neither should fail this test.
    """
    return PARAM.sub("{}", text)


def _operations() -> list[tuple[str, str]]:
    spec = app.openapi()
    found = []
    for path, item in spec["paths"].items():
        for method, _ in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                found.append((method.upper(), path))
    return sorted(found)


@pytest.fixture(scope="module")
def documented() -> str:
    return _normalise(DOC.read_text(encoding="utf-8"))


def test_every_operation_is_documented(documented: str) -> None:
    operations = _operations()
    assert operations, "no operations found in the OpenAPI document"

    missing = []
    for method, path in operations:
        normalised = _normalise(path)
        # Most project-owned routes exist twice: once resolved against the server's
        # default project and once addressed explicitly. `docs/api-reference.md`
        # documents them once, under whichever form reads better, and says so in
        # its "Project scoping" section -- so either spelling satisfies both.
        unscoped = (
            normalised.replace(SCOPE_PREFIX, "/api", 1)
            if normalised.startswith(SCOPE_PREFIX)
            else normalised
        )
        if normalised not in documented and unscoped not in documented:
            missing.append(f"{method} {unscoped}")

    assert not missing, (
        f"{len(missing)} REST operations are not mentioned in docs/api-reference.md.\n"
        "Add a row for each, or a sentence naming it:\n  " + "\n  ".join(sorted(set(missing)))
    )


def test_the_no_authentication_warning_is_near_the_top(documented: str) -> None:
    """The most consequential sentence on the page must not be buried.

    Auditor 7's point, and it is a fair one: a reader who skims the first screen and
    starts writing a client has already made the decision this sentence exists to
    inform. Fifteen lines is roughly one screen.
    """
    lines = documented.splitlines()
    for index, line in enumerate(lines[:15]):
        if "no authentication" in line.lower():
            return
    pytest.fail(
        "docs/api-reference.md must state that the API is unauthenticated within its "
        "first 15 lines; it is the fact a reader most needs before writing a client."
    )
