"""Run the repository's Python and frontend verification gates."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path) -> None:
    """Run one check and stop immediately when it fails."""
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    """Run pytest followed by the frontend's generated, lint, test, and build checks."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        print("npm is required to run the frontend checks.", file=sys.stderr)
        return 1

    try:
        run([sys.executable, "-m", "pytest"], cwd=ROOT)
        run([npm, "run", "check"], cwd=ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
