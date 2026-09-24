"""Every review sandbox serves the tailnet as well as loopback when Tailscale is up (task-567).

Until this test existed each ``scripts/*_sandbox.py`` chose its own bind address, most
chose ``127.0.0.1``, and task-562 handed the owner a review URL his phone could not open.
The first case makes the shared helper the only way a sandbox serves; the rest pin what
the helper binds with Tailscale up, down, and switched off.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SANDBOXES = sorted(SCRIPTS.glob("*_sandbox.py"))


def _serves_the_app(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "agentjobs.api.main"
        for node in ast.walk(tree)
    )


def _imports_shared_serve(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "sandbox_serve"
        and any(alias.name == "serve" for alias in node.names)
        for node in ast.walk(tree)
    )


def _mentions_uvicorn(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "uvicorn" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("uvicorn"):
            return True
    return False


@pytest.mark.parametrize("path", SANDBOXES, ids=lambda path: path.name)
def test_a_sandbox_serves_through_the_shared_helper(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not _mentions_uvicorn(tree), (
        f"{path.name} imports uvicorn itself. Serve with "
        "`from sandbox_serve import serve; serve(app, port=port)` so the review is "
        "reachable from a phone whenever Tailscale is up."
    )
    if _serves_the_app(tree):
        assert _imports_shared_serve(tree), f"{path.name} serves the app without sandbox_serve"


@pytest.fixture()
def sandbox_serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.delenv("AGENTJOBS_SANDBOX_TAILNET", raising=False)
    sys.modules.pop("sandbox_serve", None)
    yield importlib.import_module("sandbox_serve")
    sys.modules.pop("sandbox_serve", None)


def _fake_cli(monkeypatch: pytest.MonkeyPatch, module: ModuleType, *, up: bool) -> None:
    answers: dict[tuple[str, ...], str] = {
        ("ip", "-4"): "100.64.0.7\n",
        ("status", "--json"): json.dumps({"Self": {"DNSName": "box.example.ts.net."}}),
    }

    def fake(*arguments: str) -> Optional[str]:
        return answers.get(arguments) if up else None

    monkeypatch.setattr(module, "_tailscale", fake)


class _Recorder:
    """Stands in for uvicorn: records every address a server was built for."""

    def __init__(self) -> None:
        self.hosts: List[str] = []
        self.config: dict[str, Any] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import uvicorn

        recorder = self

        class Config:
            def __init__(self, app: Any, **kwargs: Any) -> None:
                recorder.hosts.append(kwargs["host"])
                recorder.config = kwargs

        class Server:
            def __init__(self, config: Config) -> None:
                self.should_exit = False

            def run(self) -> None:
                return None

        monkeypatch.setattr(uvicorn, "Config", Config)
        monkeypatch.setattr(uvicorn, "Server", Server)


def test_tailscale_up_serves_loopback_and_the_tailnet(
    sandbox_serve: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_cli(monkeypatch, sandbox_serve, up=True)
    recorder = _Recorder()
    recorder.install(monkeypatch)

    sandbox_serve.serve(object(), port=8999, lifespan="off")

    assert sorted(recorder.hosts) == ["100.64.0.7", "127.0.0.1"]
    assert recorder.config["lifespan"] == "off"
    out = capsys.readouterr().out
    assert "phone/tablet: http://box.example.ts.net:8999/app/" in out
    assert "this machine: http://127.0.0.1:8999/app/" in out


def test_tailscale_down_serves_loopback_only_and_says_why(
    sandbox_serve: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_cli(monkeypatch, sandbox_serve, up=False)
    recorder = _Recorder()
    recorder.install(monkeypatch)

    sandbox_serve.serve(object(), port=8999)

    assert recorder.hosts == ["127.0.0.1"]
    assert "Tailscale is not running" in capsys.readouterr().out


def test_switched_off_serves_loopback_only(
    sandbox_serve: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_cli(monkeypatch, sandbox_serve, up=True)
    monkeypatch.setenv("AGENTJOBS_SANDBOX_TAILNET", "0")
    recorder = _Recorder()
    recorder.install(monkeypatch)

    sandbox_serve.serve(object(), port=8999)

    assert recorder.hosts == ["127.0.0.1"]
    assert "AGENTJOBS_SANDBOX_TAILNET is off" in capsys.readouterr().out


def test_review_base_prefers_the_tailnet_name(
    sandbox_serve: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(monkeypatch, sandbox_serve, up=True)
    assert sandbox_serve.review_base(8999) == "http://box.example.ts.net:8999"
    _fake_cli(monkeypatch, sandbox_serve, up=False)
    assert sandbox_serve.review_base(8999) == "http://127.0.0.1:8999"
