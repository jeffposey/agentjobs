"""Freshly build the frontend, create release distributions, and verify the wheel."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from scripts.build_frontend import REQUIRED_FILES, build
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    # mypy resolves the file only as `scripts.build_frontend` -- `scripts` is a package
    # precisely so it has exactly one name -- and cannot see this runtime fallback.
    from build_frontend import REQUIRED_FILES, build  # type: ignore[import-not-found,no-redef]


ROOT = Path(__file__).resolve().parents[1]
WHEEL_PREFIX = "agentjobs/frontend_dist/"


def verify_wheel(wheel: Path) -> tuple[int, int]:
    """Return total and compressed bundle bytes after verifying required wheel members."""
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise RuntimeError(
            f"Release wheel must be platform-independent (py3-none-any), got {wheel.name}"
        )
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        wheel_metadata = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(wheel_metadata) != 1:
            raise RuntimeError("Built wheel must contain exactly one .dist-info/WHEEL file")
        metadata = archive.read(wheel_metadata[0]).decode("utf-8")
        if "Root-Is-Purelib: true" not in metadata or "Tag: py3-none-any" not in metadata:
            raise RuntimeError("Built wheel metadata is not platform-independent")
        required = {f"{WHEEL_PREFIX}{relative}" for relative in REQUIRED_FILES}
        missing = sorted(required - names)
        has_js = any(
            name.startswith(f"{WHEEL_PREFIX}assets/") and name.endswith(".js") for name in names
        )
        has_css = any(
            name.startswith(f"{WHEEL_PREFIX}assets/") and name.endswith(".css") for name in names
        )
        if missing or not has_js or not has_css:
            details = (
                missing
                + ([] if has_js else ["assets/*.js"])
                + ([] if has_css else ["assets/*.css"])
            )
            raise RuntimeError(f"Built wheel is missing React package data: {', '.join(details)}")
        bundle_bytes = sum(
            info.compress_size
            for info in archive.infolist()
            if info.filename.startswith(WHEEL_PREFIX)
        )
    return wheel.stat().st_size, bundle_bytes


def verify_installed_server(wheel: Path) -> None:
    """Install the wheel and exercise `agentjobs serve` with Node absent from PATH."""
    with TemporaryDirectory(prefix="agentjobs-wheel-") as directory:
        root = Path(directory)
        site = root / "site"
        project = root / "project"
        project.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--target",
                str(site),
                str(wheel),
            ],
            cwd=root,
            check=True,
        )

        env = os.environ.copy()
        env["AGENTJOBS_HOME"] = str(root / "home")
        env["AGENTJOBS_PROJECT_ROOT"] = str(project)
        env["PATH"] = str(Path(sys.executable).parent)
        env["PYTHONPATH"] = str(site)
        env["PYTHONNOUSERSITE"] = "1"
        if shutil.which("node", path=env["PATH"]) is not None:
            raise RuntimeError("Node unexpectedly remains available in the wheel runtime PATH")

        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import agentjobs; "
                    "from agentjobs.api.spa import default_frontend_dist; "
                    "print(agentjobs.__file__); print(default_frontend_dist())"
                ),
            ],
            cwd=project,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        if str(site.resolve()) not in probe.stdout:
            raise RuntimeError(
                f"Runtime imported AgentJobs outside the wheel target:\n{probe.stdout}"
            )

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from agentjobs.cli import app; app()",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            urls = (
                "/app",
                "/app/manifest.webmanifest",
                "/app/sw.js",
                "/app/icons/icon-192.png",
                "/app/icons/icon-512.png",
            )
            deadline = time.monotonic() + 15
            while True:
                try:
                    for path in urls:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}{path}", timeout=2
                        ) as response:
                            if response.status != 200 or not response.read():
                                raise RuntimeError(f"Installed server returned an empty {path}")
                    break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None:
                        output = process.stdout.read() if process.stdout else ""
                        raise RuntimeError(f"Installed server exited early:\n{output}")
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Installed server did not become ready within 15 seconds"
                        )
                    time.sleep(0.1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main() -> int:
    """Freshly build package data and fail if Poetry omits any required asset."""
    build()
    subprocess.run(["poetry", "build", "--clean"], cwd=ROOT, check=True)
    wheels = list((ROOT / "dist").glob("agentjobs-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one AgentJobs wheel, found {len(wheels)}")
    wheel_bytes, bundle_bytes = verify_wheel(wheels[0])
    verify_installed_server(wheels[0])
    print(
        f"Verified {wheels[0].name}: {wheel_bytes:,} bytes total; "
        f"{bundle_bytes:,} compressed frontend bytes; installed server passed without Node."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
