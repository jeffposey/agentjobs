"""Shared pytest fixtures for the AgentJobs suite."""

from __future__ import annotations

import os

import pytest

from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Optional

from starlette.testclient import TestClient

from agentjobs import front_door
from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.dispatch.address import ApiBaseProbe
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.credentials import verify_run_credential
from agentjobs.dispatch.peers import SESSIONS_DIR_ENV
from agentjobs.dispatch.runner import settle_supervisors
from agentjobs.front_door import SECRET_ENV
from agentjobs.principals import set_run_credential_verifier
from agentjobs.projects import HOME_ENV
from agentjobs.execution.factory import close_execution_stores
from agentjobs.store_factory import close_databases, reset_server_process

# Imported for its side effect, and before any fixture below exists: it captures the
# machine's AgentJobs home at import, which is the only moment `isolate_project_registry`
# has not yet re-pointed it. The corpus checks read the real backlog through that capture
# rather than through the environment (task-411).
import corpus_source  # noqa: E402,F401

# The shared write-guard matrix holds assertions but is imported by the two hook test
# modules rather than collected, so pytest would not rewrite them and a failure would
# report a bare `assert False`. Registering it here, before anything imports it, keeps
# the diagnostics.
pytest.register_assert_rewrite("task_write_guard_matrix")


@pytest.fixture(autouse=True)
def isolate_session_roster(tmp_path_factory, monkeypatch) -> Iterator[Path]:
    """Point the live-session roster at an empty temp directory for every test.

    The roster is machine-level -- ``~/.claude/sessions`` -- and lists whatever Claude Code
    sessions happen to be running, the dispatched agent running the suite included. Two
    things read it and both would otherwise be decided by the state of the machine:
    ``runner.choose_session_name`` asks whether a name is taken, and the wake asks whether
    a session is still up. A test asserting either would go green or red for reasons
    nothing in the suite controls.

    Empty is also the honest default: no session under test is really running. A test that
    wants a roster writes registrations into the directory this yields, or passes
    ``sessions_dir=`` to ``peers.roster`` directly.
    """
    directory = tmp_path_factory.mktemp("claude-sessions")
    monkeypatch.setenv(SESSIONS_DIR_ENV, str(directory))
    yield directory


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
def no_front_door_by_default(monkeypatch) -> Iterator[None]:
    """No proxy secret, and no memory of one, at the start of every test (task-244).

    ``agentjobs.front_door`` caches its file read for a few seconds, and the file lives
    under ``AGENTJOBS_HOME`` -- which the fixture above re-points per test. Without this
    reset a cached answer would outlive the home it was read from, which is the kind of
    coupling that shows up as one test failing only when another ran first.

    Clearing the environment variable as well means a machine with a real front-door
    secret injected into the developer's shell does not quietly change what the suite
    proves.
    """
    monkeypatch.delenv(SECRET_ENV, raising=False)
    front_door.reset_cache()
    yield
    front_door.reset_cache()


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
def never_reads_the_machines_processes(monkeypatch) -> None:
    """No session stop reads the real process table, and no census fires (task-548).

    A cancel now reads the session's process tree and ends what outlives the stop. A test
    cancelling a fake session must never reach a real one -- the dispatched agent running
    this suite has a session tree too -- so the reader is an empty table unless a test
    installs its own. The automatic census is off for the same reason, and so that a
    machine short of memory does not change what a poller test observes.
    """
    from agentjobs.dispatch import ledger

    monkeypatch.setattr(ledger, "SESSION_TREE_READER", lambda: [])
    monkeypatch.setenv("AGENTJOBS_MEMORY_WATCH", "off")
    monkeypatch.delenv("AGENTJOBS_MEMORY_FLOOR_MB", raising=False)


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
    # And no simulated gate indexes itself in the store (task-472): `check.main` is
    # called with `subprocess.run` stubbed, and each such run would otherwise PUT a
    # `gate_run` row at whatever service answers on this machine.
    monkeypatch.setenv("AGENTJOBS_GATE_HISTORY", "off")
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


@pytest.fixture(autouse=True)
def no_database_survives_a_test() -> Iterator[None]:
    """Close every SQLite store a test opened, and forget the server declaration.

    ``store_factory`` caches one ``Database`` per path for the life of the process,
    which is exactly right in a server and exactly wrong in a test runner: the next
    test's ``AGENTJOBS_HOME`` is a different temp directory, but a cached handle on the
    previous one would still be handed out for any path that repeated. Closing also
    releases the file so Windows can delete the temp tree.

    It resets the server declaration for the same reason: a test that entered
    ``server_process()`` and failed inside it would otherwise leave every later test
    believing it was the server, which would silently disarm the one rule the factory
    exists to enforce.
    """
    yield
    # A test that dispatched a batch run leaves its supervisor thread writing, and
    # closing the store under one loses the run's terminal entry to an exception
    # nobody awaits. The served application does the same thing at shutdown and for
    # the same reason, so this is that code rather than a second copy of it (task-505).
    settle_supervisors()
    close_databases()
    close_execution_stores()
    reset_server_process()


# ---------------------------------------------------------------------------
# A hand-run parallel pytest obeys the gate's core budget too (task-536).
# ---------------------------------------------------------------------------

_GATE_SLOTS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gate_slots.py"


def _load_gate_slots() -> Optional[ModuleType]:
    """``scripts/gate_slots.py``, or ``None`` if it cannot be loaded.

    By path rather than by import, because ``scripts/`` is not a package and putting it
    on ``sys.path`` would make every other script in it importable as a side effect of
    running the suite. ``None`` is a supported outcome: the budget is an optimisation
    and this file is the test suite's, so nothing here may stop a test run.
    """
    try:
        import importlib.util

        import sys

        name = "agentjobs_gate_slots"
        spec = importlib.util.spec_from_file_location(name, _GATE_SLOTS_PATH)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # In ``sys.modules`` *before* it executes, because `@dataclass` resolves a
        # string annotation by looking its own module up there -- and this file has
        # ``from __future__ import annotations``, so every annotation is a string.
        # Without this the module fails to load and the budget silently reverts to
        # every core, which is the failure it exists to prevent.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - see the docstring
        return None


gate_slots: Any = _load_gate_slots()
_held_slot: Any = None


@pytest.fixture(autouse=True)
def the_budget_forgets_a_failed_acquire() -> Iterator[None]:
    """Clear ``gate_slots._DEGRADED`` around every test.

    It is process-wide by design -- the acquire and the ``-n`` resolution minutes later
    have no call path between them -- and a process-wide flag is exactly what leaks from
    one test into the next one xdist happens to schedule beside it. A test that wants it
    set sets it.
    """
    if gate_slots is not None:
        gate_slots.reset_degraded()
    yield
    if gate_slots is not None:
        gate_slots.reset_degraded()


def a_slot_is_wanted(config: Any, workers: int) -> bool:
    """Whether this pytest process should take a gate slot of its own.

    Three things disqualify it. **A serial run** costs one core and is not what the
    capacity protects -- ``pytest -k one_test`` must never wait for anything. **A run
    inside a gate** is already covered by the slot the gate is holding, which it says so
    through ``gate_slots.SLOT_ENV``; taking a second would have one gate counted twice.
    **An xdist worker** is one of the processes the controller's slot already paid for.
    Collection-only is excluded too: it starts no workers whatever ``-n`` says.
    """
    if gate_slots is None or workers <= 1:
        return False
    if hasattr(config, "workerinput"):
        return False
    if config.getoption("collectonly", False):
        return False
    return not gate_slots.held_by_an_enclosing_gate()


def take_a_slot(config: Any, workers: int) -> None:
    """Queue for a slot and hold it, at most once per process, never raising."""
    global _held_slot
    if _held_slot is not None or not a_slot_is_wanted(config, workers):
        return
    try:
        _held_slot = gate_slots.acquire(Path(__file__).resolve().parents[1])
    except Exception:  # noqa: BLE001 - a slot is an optimisation; the suite is not
        _held_slot = None


@pytest.hookimpl
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """What ``-n auto`` means here: the gate's budget, not every core.

    ``gate_slots`` only ever saw ``scripts/check.py``, so an agent running
    ``pytest -n auto`` by hand took all 32 cores and was invisible to every gate on the
    machine while doing it. xdist 3.8 has this hook and calls it from its own
    ``pytest_cmdline_main``, before anything else in a session -- which is the right
    moment, because the slot has to be taken *before* the budget is resolved for the run
    to count itself.

    Returning a number is not optional here: this hook is ``firstresult``, so a ``None``
    would fall through to xdist's own implementation and the reserve would be lost.
    """
    take_a_slot(config, workers=2)
    try:
        return gate_slots.budget() if gate_slots is not None else (os.cpu_count() or 1)
    except Exception:  # noqa: BLE001 - never fail a run over the budget
        return os.cpu_count() or 1


@pytest.hookimpl
def pytest_configure(config: pytest.Config) -> None:
    """The explicit ``-n 8`` case, which never reaches the hook above.

    By ``pytest_configure`` xdist's ``pytest_cmdline_main`` has already turned ``auto``
    into a number, so ``numprocesses`` is an int either way and this is idempotent with
    the acquire above.
    """
    count = getattr(config.option, "numprocesses", None)
    take_a_slot(config, workers=count if isinstance(count, int) else 0)


@pytest.hookimpl
def pytest_unconfigure(config: pytest.Config) -> None:
    """Give the slot back. A missed release expires within ``STALE_SECONDS`` anyway."""
    global _held_slot
    if _held_slot is not None:
        try:
            _held_slot.release()
        except Exception:  # noqa: BLE001 - releasing is best-effort by construction
            pass
        _held_slot = None
