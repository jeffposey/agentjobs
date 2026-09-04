"""Shared pytest fixtures for the AgentJobs suite."""

from __future__ import annotations

import pytest

from typing import Iterator

from starlette.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.dispatch.address import ApiBaseProbe
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.credentials import verify_run_credential
from agentjobs.principals import set_run_credential_verifier
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
def the_production_run_credential_verifier() -> Iterator[None]:
    """Start every test with the verifier the served application installs (task-331).

    The verifier is a module global, and several tests legitimately swap it -- for a
    fake, or for the no-op default -- inside a ``try/finally``. Restoring it here rather
    than in each of those makes the restore the same in all of them and makes the order
    tests happen to run in stop mattering: without this, a test that reset to the no-op
    would silently disarm run resolution for everything that followed it in that worker.
    """
    set_run_credential_verifier(verify_run_credential)
    yield
    set_run_credential_verifier(verify_run_credential)


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

    The credential (task-331) is scrubbed here too, for a related reason: a test that
    inherited a live run's credential would have every ``TaskClient`` it builds resolve
    as that run instead of as the owner, so the suite would be measuring the ambient
    environment rather than the state it set up.
    """
    for name in ("AGENTJOBS_RUN_ID", "AGENTJOBS_RUN_DIR", "AGENTJOBS_RUN_CREDENTIAL"):
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


@pytest.fixture(autouse=True)
def the_test_client_arrives_on_loopback(monkeypatch) -> None:
    """Give every ``TestClient`` a loopback peer, unless the test names its own.

    Starlette's ``TestClient`` defaults its peer address to the literal host
    ``"testclient"``, which is not an IP address at all. ``principals.is_front_door``
    therefore -- correctly, and by design -- says that request did not arrive by the
    path the front door controls, so **no principal resolves**, so with task-332's
    capability gate installed every mutating route in this suite would answer 403.

    That is a fact about the harness rather than about the application: AgentJobs is
    served on loopback, so a request the application really sees carries a loopback
    peer. This fixture makes the harness say what the deployment does. It is a default,
    not an override: ``tests/test_identity_registry_api.py`` and the capability tests
    pass their own ``client=`` to say where a request came from, and those win.

    The alternative -- treating an unparseable client address as loopback in
    ``is_front_door`` -- was rejected outright. "We could not tell where this came from"
    must never mean "believe what it claims about itself", and weakening that rule to
    spare a fixture would put a hole in the one function the whole trust model rests on.
    """
    original = TestClient.__init__

    def on_loopback(self: TestClient, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(TestClient, "__init__", on_loopback)
