"""Build and validate the React package data before Poetry creates a distribution."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BUNDLE = ROOT / "src" / "agentjobs" / "frontend_dist"
REQUIRED_FILES = (
    "index.html",
    "manifest.webmanifest",
    "sw.js",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/icon-maskable-512.png",
)


def validate_bundle(bundle: Path = BUNDLE) -> None:
    """Reject incomplete output before Poetry can place it in a distribution."""
    missing = [relative for relative in REQUIRED_FILES if not (bundle / relative).is_file()]
    assets = bundle / "assets"
    if not assets.is_dir() or not any(assets.glob("*.js")) or not any(assets.glob("*.css")):
        missing.append("assets/*.js and assets/*.css")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"React release bundle is incomplete; missing: {joined}")


def build() -> None:
    """Reinstall the locked toolchain and produce a fresh package-data directory."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "Node and npm are required to build an AgentJobs release, but never to "
            "install or run its wheel."
        )
    subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)

    validate_bundle()
    files = [path for path in BUNDLE.rglob("*") if path.is_file()]
    print(
        f"Validated React package data: {len(files)} files, "
        f"{sum(path.stat().st_size for path in files):,} bytes."
    )


if __name__ == "__main__":
    build()
