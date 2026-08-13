"""Static contracts for the installable React application."""

from __future__ import annotations

import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_manifest_declares_standalone_scope_and_reproducible_icons() -> None:
    manifest = json.loads((FRONTEND / "public" / "manifest.webmanifest").read_text())

    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/app/"
    assert manifest["scope"] == "/app/"
    assert [(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]] == [
        ("192x192", "any"),
        ("512x512", "any"),
        ("512x512", "maskable"),
    ]
    assert png_size(FRONTEND / "public" / "icons" / "icon-192.png") == (192, 192)
    assert png_size(FRONTEND / "public" / "icons" / "icon-512.png") == (512, 512)
    assert png_size(FRONTEND / "public" / "icons" / "icon-maskable-512.png") == (512, 512)


def test_service_worker_keeps_task_api_network_only() -> None:
    worker = (FRONTEND / "src" / "service-worker.js").read_text()
    api_branch = worker.split('url.pathname.startsWith("/api/")', 1)[1].split(
        "if (request.mode", 1
    )[0]

    assert "fetch(request)" in api_branch
    assert "caches.match" not in api_branch
    assert 'caches.match("/app/")' in worker
    assert "self.skipWaiting()" in worker
    assert "self.clients.claim()" in worker


def test_build_injects_hashed_application_assets_into_the_shell_cache() -> None:
    builder = (FRONTEND / "scripts" / "build-service-worker.mjs").read_text()

    assert '"/app/"' in builder
    assert r"\/app\/assets\/" in builder
    assert "...builtAssets" in builder
    assert "agentjobs-shell-${revision}" in builder
