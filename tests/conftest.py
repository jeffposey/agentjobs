"""Shared pytest fixtures for the AgentJobs suite."""

from __future__ import annotations

import pytest

from typing import Iterator

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.dispatch.address import ApiBaseProbe
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
