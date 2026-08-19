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


def package_origin() -> Path | None:
    """Return the file this interpreter would import agentjobs from, if any."""
    spec = importlib.util.find_spec("agentjobs")
    if spec is None or spec.origin is None:
        return None
    return Path(spec.origin).resolve()


def setup_problems(root: Path, origin: Path | None) -> list[str]:
    """Name whatever stops this checkout from verifying its own code.

    A fresh worktree has neither dependency tree, and without this the gate fails
    deep inside pytest's collection or npm's resolver, which reads like a broken
    repository rather than an unfinished setup.

    The import is judged by *location*, not by importability: another checkout's
    agentjobs on this interpreter's path answers `find_spec` perfectly well, and the
    suite would then pass on source this branch does not contain.
    """
    problems = []

    if origin is None:
        problems.append("the agentjobs package is not installed")
    elif root.resolve() not in origin.parents:
        problems.append(f"agentjobs imports from {origin.parent}, outside this checkout")

    if not (root / "frontend" / "node_modules").is_dir():
        problems.append("frontend/node_modules is missing")

    return problems


def main() -> int:
    """Run the Python checks, then the frontend's generated, lint, test and build ones.

    Format, lint and types run before pytest: together they take about seven seconds
    and they fail on things pytest will never notice, so paying four minutes to find a
    misformatted file is the wrong order.

    They are in the gate rather than only in ENGINEERING.md's pre-commit list because
    that list is documentation of an intention and this is the thing anyone actually
    runs. Task-166 found `poetry run mypy .` aborting on a module-name collision before
    it checked a single file -- it had never type-checked a line of this repository --
    and a `black` drift on `main`, both of which had survived precisely because nothing
    enforced them.
    """
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        print("npm is required to run the frontend checks.", file=sys.stderr)
        return 1

    problems = setup_problems(ROOT, package_origin())
    if problems:
        print(
            f"This checkout cannot verify itself: {'; '.join(problems)}.\n"
            "Run `python scripts/bootstrap.py`, then `poetry run python scripts/check.py`.",
            file=sys.stderr,
        )
        return 1

    try:
        run([sys.executable, "-m", "black", "--check", "."], cwd=ROOT)
        run([sys.executable, "-m", "ruff", "check", "."], cwd=ROOT)
        run([sys.executable, "-m", "mypy", "."], cwd=ROOT)
        run([sys.executable, "-m", "pytest"], cwd=ROOT)
        run([npm, "run", "check"], cwd=ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
