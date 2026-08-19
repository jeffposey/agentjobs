"""Run the repository's Python and frontend verification gates."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path) -> None:
    """Run one check and stop immediately when it fails."""
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def unbootstrapped() -> list[str]:
    """Name what a never-bootstrapped checkout is missing.

    A fresh worktree has neither, and without this the gate fails deep inside
    pytest's collection or npm's resolver, which reads like a broken repository
    rather than an unfinished setup.
    """
    return [
        name
        for name, present in (
            ("the agentjobs package", importlib.util.find_spec("agentjobs") is not None),
            ("frontend/node_modules", (ROOT / "frontend" / "node_modules").is_dir()),
        )
        if not present
    ]


def main() -> int:
    """Run pytest followed by the frontend's generated, lint, test, and build checks."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        print("npm is required to run the frontend checks.", file=sys.stderr)
        return 1

    missing = unbootstrapped()
    if missing:
        print(
            f"This checkout is missing {' and '.join(missing)}. "
            "Run `python scripts/bootstrap.py` first.",
            file=sys.stderr,
        )
        return 1

    try:
        run([sys.executable, "-m", "pytest"], cwd=ROOT)
        run([npm, "run", "check"], cwd=ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
