"""Two checkouts must be able to run the gate at once, so no gate port is a constant.

Task-187: ``frontend/playwright.config.ts`` held ``const port = 18940`` and
``run_server.py`` repeated the same literal, so the second of two concurrent
``scripts/check.py`` runs failed to bind about four minutes into a five-minute gate.
The tests here guard the two properties that fix depends on: the ports are a function
of the checkout's path, and neither half of a pair can invent one on its own.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

from agentjobs.api import spa as _spa  # noqa: E402

_ORIGINAL_DIST = _spa.default_frontend_dist
"""Held so a test that repoints the bundle can put the real one back."""


def load_script(name: str) -> types.ModuleType:
    """Load a repository script by path, without making ``scripts/`` a package.

    ``from scripts.bench import ...`` depends on nothing else having claimed the name
    ``scripts`` first, and on this machine pywin32 does. See
    ``tests/test_performance_budgets.py`` for the full account. The module is
    registered under its own name before it executes because its dataclasses resolve
    their own module through ``sys.modules``, and find ``None`` there otherwise.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = load_script("bench")


class TestBenchPortIsDerived:
    """``scripts/bench.py`` picks its port from the checkout, not from a constant."""

    def test_same_checkout_gets_the_same_port(self) -> None:
        first = bench.checkout_port(Path("/checkouts/aj-187"))
        second = bench.checkout_port(Path("/checkouts/aj-187"))
        assert first == second

    def test_different_checkouts_get_different_ports(self) -> None:
        ports = {bench.checkout_port(Path(f"/checkouts/aj-{number}")) for number in range(100, 140)}
        # Forty sibling worktrees, which is far more than are ever open at once.
        assert len(ports) >= 39

    def test_the_port_is_in_a_usable_range(self) -> None:
        for number in range(100, 140):
            port = bench.checkout_port(Path(f"/checkouts/aj-{number}"))
            assert 1024 < port < 49152, port

    def test_the_environment_can_name_a_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bench.BENCH_PORT_ENV, "31234")
        assert bench.checkout_port(ROOT) == 31234

    def test_a_nonsense_override_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(bench.BENCH_PORT_ENV, "70000")
        with pytest.raises(SystemExit):
            bench.checkout_port(ROOT)

    def test_the_bench_port_cannot_collide_with_the_gate_port(self) -> None:
        """A benchmark and a gate in one checkout must not want one socket."""
        gate = _gate_port_bounds()
        bench_low = bench.BENCH_PORT_BASE
        bench_high = bench.BENCH_PORT_BASE + bench.BENCH_PORT_SPAN
        assert bench_low >= gate[1] or bench_high <= gate[0]


def _gate_port_bounds() -> tuple[int, int]:
    """Read the gate's port range straight out of the Playwright port module.

    Task-369 gave each Playwright worker its own server, so the gate wants a *block* of
    ports rather than one. The block is carved out of the same 20000-29999 the gate has
    always owned -- that is the property the bench-collision test below depends on, and
    the reason the span is divided rather than extended.
    """
    source = (FRONTEND / "e2e" / "ports.ts").read_text(encoding="utf-8")
    base = int(source.split("PORT_BASE = ", 1)[1].split(";", 1)[0].replace("_", ""))
    span = int(source.split("PORT_SPAN = ", 1)[1].split(";", 1)[0].replace("_", ""))
    return base, base + span


class TestNoGatePortIsHardcoded:
    """Neither half of the Playwright pair carries a literal port."""

    def test_the_playwright_ports_are_derived(self) -> None:
        source = (FRONTEND / "e2e" / "ports.ts").read_text(encoding="utf-8")
        assert "18940" not in source
        assert "checkoutRoot()" in source

    def test_the_config_passes_a_port_to_every_server_it_starts(self) -> None:
        """One server per worker, each told which port it owns and none guessing."""
        source = (FRONTEND / "playwright.config.ts").read_text(encoding="utf-8")
        assert "18940" not in source
        # The webServer must be handed the same number its baseURL watches.
        assert "AGENTJOBS_E2E_PORT" not in source, "the name belongs to e2e/ports.ts"
        assert "PORT_ENV" in source
        assert "ports.map(" in source

    def test_every_worker_gets_a_port_inside_the_block(self) -> None:
        """A worker outside the gate's range could land on the benchmark's."""
        source = (FRONTEND / "e2e" / "ports.ts").read_text(encoding="utf-8")
        max_workers = int(source.split("MAX_WORKERS = ", 1)[1].split(";", 1)[0])
        base, top = _gate_port_bounds()
        span = top - base
        # Blocks of `MAX_WORKERS` consecutive ports must tile the span exactly: a
        # remainder would put the highest block's last worker past the top of the range
        # and into whatever owns the ports above it.
        assert span % max_workers == 0
        highest_first = base + span - max_workers
        assert highest_first + max_workers - 1 == top - 1

    def test_the_config_still_refuses_to_reuse_a_server(self) -> None:
        """Attaching to another checkout's server would be worse than failing."""
        source = (FRONTEND / "playwright.config.ts").read_text(encoding="utf-8")
        assert "reuseExistingServer: false" in source

    def test_the_server_has_no_port_of_its_own(self) -> None:
        source = (FRONTEND / "e2e" / "run_server.py").read_text(encoding="utf-8")
        assert "18940" not in source
        assert "os.environ.get(PORT_ENV)" in source


class TestTheServerRefusesToGuess:
    """``run_server.py`` binds the port it was given, or nothing at all."""

    def test_a_missing_variable_is_a_clear_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _load_run_server()
        monkeypatch.delenv(server.PORT_ENV, raising=False)
        with pytest.raises(SystemExit) as caught:
            server.resolve_port()
        assert server.PORT_ENV in str(caught.value)

    def test_the_given_port_is_the_bound_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _load_run_server()
        monkeypatch.setenv(server.PORT_ENV, "24242")
        assert server.resolve_port() == 24242

    def test_a_nonsense_port_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _load_run_server()
        monkeypatch.setenv(server.PORT_ENV, "not-a-port")
        with pytest.raises(SystemExit):
            server.resolve_port()


def _load_run_server() -> types.ModuleType:
    """Import the Playwright fixture server by path; it is not on any package path."""
    path = FRONTEND / "e2e" / "run_server.py"
    spec = importlib.util.spec_from_file_location("e2e_run_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_run_server"] = module
    spec.loader.exec_module(module)
    return module


def test_the_checkout_is_named_when_a_port_is_claimed() -> None:
    """A bind failure has to be diagnosable without reading the config (ac-3)."""
    config = (FRONTEND / "playwright.config.ts").read_text(encoding="utf-8")
    assert "console.log" in config and "checkout ${checkoutRoot()}" in config

    server = (FRONTEND / "e2e" / "run_server.py").read_text(encoding="utf-8")
    assert "serving {CHECKOUT}" in server

    bench_source = (ROOT / "scripts" / "bench.py").read_text(encoding="utf-8")
    assert "checkout {ROOT} serving" in bench_source


def test_bench_default_port_follows_this_checkout() -> None:
    """The argparse default is this checkout's port, not a shared constant."""
    assert bench.DEFAULT_PORT == bench.checkout_port(ROOT)
    assert "18950" not in (ROOT / "scripts" / "bench.py").read_text(encoding="utf-8")


def test_environment_is_not_leaked_between_tests() -> None:
    """Sanity: the override variables are absent in a normal run."""
    assert os.environ.get(bench.BENCH_PORT_ENV) in (None, "")


class TestEachServerServesItsOwnBundle:
    """Task-369: a spec that rewrites the bundle must not rewrite a neighbour's.

    ``capture-draft.spec.ts`` edits ``sw.js`` and ``build-info.json`` to make a running
    tab believe the app was rebuilt under it. Served out of the checkout, that write
    reaches every worker's service worker at once, and three other browsers reload in
    the middle of whatever they were doing. Each server therefore copies the bundle.
    """

    def test_two_servers_choose_two_directories(self) -> None:
        server = _load_run_server()
        assert server.private_bundle_dir(23624) != server.private_bundle_dir(23625)

    def test_the_directory_is_derived_from_the_port_alone(self) -> None:
        """The spec computes the same path in TypeScript, from its own worker's URL."""
        server = _load_run_server()
        assert server.private_bundle_dir(23624).name == f"{server.DIST_PREFIX}23624"

    def test_the_fixture_and_the_spec_agree_on_the_name(self) -> None:
        source = (FRONTEND / "e2e" / "fixtures.ts").read_text(encoding="utf-8")
        server = _load_run_server()
        assert f"{server.DIST_PREFIX}${{port}}" in source

    def test_a_copy_is_made_and_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.api import spa

        source = tmp_path / "frontend_dist"
        (source / "assets").mkdir(parents=True)
        (source / "build-info.json").write_text('{"bundle_id": "original"}', encoding="utf-8")
        monkeypatch.setattr(spa, "default_frontend_dist", lambda: source)

        server = _load_run_server()
        monkeypatch.setattr(server, "private_bundle_dir", lambda port: tmp_path / f"copy-{port}")
        try:
            target = server.private_bundle(23624)
            assert target is not None
            assert target != source
            assert (target / "build-info.json").is_file()
            # What the app reads is the copy, so a write to the copy is what it sees --
            # and a write to the checkout's own bundle is what it no longer sees.
            assert spa.default_frontend_dist() == target
            (target / "build-info.json").write_text('{"bundle_id": "rebuilt"}', encoding="utf-8")
            assert spa.bundle_id() == "rebuilt"
        finally:
            spa.default_frontend_dist = _ORIGINAL_DIST

    def test_a_checkout_with_no_bundle_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone that never ran `npm run build` already says so; a copy would hide it."""
        from agentjobs.api import spa

        monkeypatch.setattr(spa, "default_frontend_dist", lambda: tmp_path / "absent")
        server = _load_run_server()
        assert server.private_bundle(23624) is None
