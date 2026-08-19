"""Prepare a fresh checkout to run the repository's verification gate.

`git worktree add` and `git clone` copy tracked files only, and both things
`scripts/check.py` needs are untracked: the Poetry virtualenv and
`frontend/node_modules`. An agent that follows ALLAGENTS.md and takes its own
worktree before writing anything therefore lands somewhere that cannot verify its
own work, which is how an undocumented workaround -- borrowing another checkout's
environment -- gets invented under time pressure.

This installs both, makes sure the browser the end-to-end test drives is present,
and then proves the environment imports *this* checkout rather than the clone next
door.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path) -> None:
    """Run one bootstrap step and stop immediately when it fails."""
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def executable(*names: str) -> str | None:
    """Return the first of `names` found on PATH, so Windows shims resolve too."""
    for name in names:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def verify_environment(poetry: str) -> str | None:
    """Return an explanation if the environment does not import this checkout.

    Poetry keys its virtualenvs on the project path and installs the root package
    in editable mode, so a correctly bootstrapped checkout imports its own `src/`.
    Borrowing a different checkout's environment silently imports *that* checkout,
    and a green suite then says nothing about the branch under test. The failure is
    invisible in the test output, so it is worth one subprocess to rule out.
    """
    probe = "import agentjobs, sys; sys.stdout.write(agentjobs.__file__)"
    result = subprocess.run(
        [poetry, "run", "python", "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"The environment cannot import agentjobs:\n{result.stderr.strip()}"

    imported = Path(result.stdout.strip()).resolve()
    expected = (ROOT / "src" / "agentjobs").resolve()
    if expected not in imported.parents:
        return (
            f"The environment imports {imported}, which is outside {ROOT}.\n"
            "Tests run here would exercise another checkout's source and report a "
            "result that says nothing about this branch."
        )

    print(f"\nImports resolve to {imported.parent} -- this checkout's own source.")
    return None


def main() -> int:
    """Install the Python and frontend dependencies, then verify the result."""
    poetry = executable("poetry.exe", "poetry")
    npm = executable("npm.cmd", "npm")
    npx = executable("npx.cmd", "npx")

    absent = [
        name for name, found in (("poetry", poetry), ("npm", npm), ("npx", npx)) if found is None
    ]
    if absent:
        print(f"Not on PATH: {', '.join(absent)}.", file=sys.stderr)
        return 1
    assert poetry is not None and npm is not None and npx is not None

    started = time.monotonic()
    try:
        run([poetry, "install"], cwd=ROOT)
        run([npm, "ci"], cwd=ROOT / "frontend")
        # Idempotent and about a second once the browser is in the user-level
        # cache, which the end-to-end step of `npm run check` drives.
        run([npx, "playwright", "install", "chromium"], cwd=ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        return exc.returncode

    failure = verify_environment(poetry)
    if failure is not None:
        print(failure, file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"\nBootstrapped {ROOT} in {elapsed:.0f}s.")
    print("Verify with: poetry run python scripts/check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
