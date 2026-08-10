"""Shared pytest fixtures for the AgentJobs suite."""

from __future__ import annotations

import pytest

from typing import Iterator

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.projects import HOME_ENV


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
