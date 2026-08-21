"""Shared pytest fixtures for the AgentJobs suite."""

from __future__ import annotations

import pytest

from typing import Iterator

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.dispatch.address import ApiBaseProbe
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.projects import HOME_ENV

# The shared write-guard matrix holds assertions but is imported by the two hook test
# modules rather than collected, so pytest would not rewrite them and a failure would
# report a bare `assert False`. Registering it here, before anything imports it, keeps
# the diagnostics.
pytest.register_assert_rewrite("task_write_guard_matrix")


@pytest.fixture(autouse=True)
def isolate_project_registry(tmp_path_factory, monkeypatch) -> Iterator[None]:
    """Point the project registry at a temp directory for every test.

    The registry is machine-level: it defaults to ``~/.agentjobs/projects.yaml``. Any
    test that runs ``agentjobs init``, or otherwise registers a project, would
    otherwise write the developer's real home directory -- which happened, and put a
    pytest tmp path into a live registry. Autouse, because the cost of forgetting is
    silent pollution of state outside the repo rather than a failing test.

    Tests that need their own registry can still override AGENTJOBS_HOME; this only
    guarantees the default is never the real one.
    """
    monkeypatch.setenv(HOME_ENV, str(tmp_path_factory.mktemp("agentjobs-home")))
    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture(autouse=True)
def never_inside_a_dispatched_run(monkeypatch) -> None:
    """Detach every test from any dispatched run this process happens to belong to.

    ``agentjobs.dispatch.phases`` writes a phase record when ``AGENTJOBS_RUN_DIR`` names
    a directory, and this suite exercises ``scripts/check.py``'s ``main`` a dozen times
    over. Run inside a real dispatched run -- which is exactly where the gate runs -- each
    of those simulated gates appended a record to that run's ``phases.jsonl``, so the
    measurement the records exist for was reading sixteen phantom gate runs beside one
    real one.

    ``scripts/check.py`` scrubs the pair for its own children, which covers the gate.
    This covers a bare ``pytest`` too, and is the guarantee that does not depend on how
    the suite was started. A test about the records sets the variables itself.
    """
    for name in ("AGENTJOBS_RUN_ID", "AGENTJOBS_RUN_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_claude_home(tmp_path_factory, monkeypatch) -> None:
    """Point the expired-login check at an empty directory for every test.

    dispatch.auth reads Claude Code's own home -- ~/.claude -- to find a
    session transcript. Left alone, the poller tests would ask the developer machine
    whether *its* sessions had died, which is both a read outside the repository and a
    suite whose result depends on whose laptop it runs on. Autouse for the same reason
    the two fixtures around it are.

    A test that wants a transcript writes one and re-points this; the later setenv
    wins.
    """
    monkeypatch.setenv(CLAUDE_HOME_ENV, str(tmp_path_factory.mktemp("claude-home")))


PROBE_CALL_SITES = (
    "agentjobs.dispatch.address.probe_api_base",
    "agentjobs.dispatch.guards.probe_api_base",
    "agentjobs.cli.probe_api_base",
)
"""Every name the probe is reachable by. Imported by name, so each binding is its own."""


@pytest.fixture(autouse=True)
def api_base_always_answers(monkeypatch) -> None:
    """Make the dispatch reachability gate say yes, for every test that does not opt out.

    Dispatch refuses to spawn a run whose agent would be told an address nothing serves
    (task-193). Left alone, that check makes a real TCP connection to whatever the test
    home resolved -- usually ``http://localhost:8765``, which on a developer's machine
    is either refused (so every dispatch test fails) or, worse, answered by their own
    running server (so the suite depends on it).

    Autouse for the same reason ``isolate_project_registry`` is: the cost of forgetting
    is not a failing test but a suite that reaches outside itself. Tests *of* the probe
    and the gate re-patch these same names, which wins over this.
    """

    def answered(api_base: str, **_: object) -> ApiBaseProbe:
        return ApiBaseProbe(
            api_base=api_base,
            answered=True,
            is_agentjobs=True,
            detail="AgentJobs answered (stubbed by tests/conftest.py)",
        )

    for target in PROBE_CALL_SITES:
        monkeypatch.setattr(target, answered)
