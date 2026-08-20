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

It also refuses to install into somebody else's environment, which is the harder half.
Poetry prefers an **activated** virtualenv over the one it keys on the project path, so
a shell with `VIRTUAL_ENV` pointing at the main clone -- which is what a dispatched agent
inherits on this machine -- turns a worktree's `poetry install` into a rewrite of the main
clone's editable install. The clone then serves an unmerged branch's code from the right
files, and says nothing at all about it. That is task-194, and it was caused by running
this script exactly as documented.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# What tells Poetry to install into an already-activated environment rather than into
# the one it derives from the project's path.
ACTIVATION_VARS = ("VIRTUAL_ENV", "POETRY_ACTIVE")

IMPORT_PROBE = "import agentjobs, sys; sys.stdout.write(agentjobs.__file__)"


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run one bootstrap step and stop immediately when it fails."""
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def executable(*names: str) -> str | None:
    """Return the first of `names` found on PATH, so Windows shims resolve too."""
    for name in names:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def detached_environment() -> dict[str, str]:
    """This process's environment with any activated virtualenv removed."""
    return {key: value for key, value in os.environ.items() if key not in ACTIVATION_VARS}


def checkout_of(module_file: str) -> Path | None:
    """Return the checkout an imported `agentjobs.__file__` belongs to, if any.

    A checkout is `<root>/src/agentjobs` beside a `<root>/pyproject.toml`. A package
    unpacked into `site-packages` has no checkout behind it and is not something this
    script needs an opinion about.
    """
    package_dir = Path(module_file).resolve().parent
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parents[1]
    return root if (root / "pyproject.toml").is_file() else None


def poetry_query(poetry: str, args: list[str], env: dict[str, str] | None) -> str | None:
    """Ask Poetry something, returning None rather than raising when it cannot answer."""
    result = subprocess.run(
        [poetry, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    answer = result.stdout.strip()
    return answer or None


def imported_checkout(poetry: str, env: dict[str, str] | None) -> Path | None:
    """Which checkout the environment Poetry would use currently imports, if any."""
    module_file = poetry_query(poetry, ["run", "python", "-c", IMPORT_PROBE], env)
    return None if module_file is None else checkout_of(module_file)


def install_environment(poetry: str) -> tuple[dict[str, str] | None, str | None]:
    """Choose the environment to run Poetry in, refusing to hijack another checkout's.

    Returns the environment to pass to every Poetry call -- None meaning "inherit this
    process's" -- and a line to print when it was changed.

    The narrow case this detaches from: a virtualenv is activated, and it holds an
    editable install of a *different* checkout. Installing there would repoint that
    checkout's environment at this one, which is the exact failure in task-194 and is
    invisible from either side afterwards. Detaching sends Poetry to this checkout's own
    path-keyed environment, so the worktree gets what it needed and the other checkout is
    left alone.

    Deliberately not detached: an activated environment that imports *this* checkout, or
    imports nothing yet. The first is the main clone repairing itself, which must keep
    working -- it is the documented repair for a `.pth` a worktree already rewrote. The
    second is somebody's own `python -m venv .venv`, which they activated on purpose and
    which this script has no business overruling.
    """
    if not any(os.environ.get(key) for key in ACTIVATION_VARS):
        return None, None

    occupant = imported_checkout(poetry, None)
    if occupant is None or occupant == ROOT:
        return None, None

    active = os.environ.get("VIRTUAL_ENV", "an activated environment")
    return detached_environment(), (
        f"Ignoring the activated virtualenv {active}.\n"
        f"It holds an editable install of {occupant}, so installing there would repoint "
        f"that checkout at this one and leave its own server running this branch's code "
        f"(task-194).\nUsing this checkout's own Poetry environment instead."
    )


def verify_environment(poetry: str, env: dict[str, str] | None) -> str | None:
    """Return an explanation if the environment does not import this checkout.

    Poetry keys its virtualenvs on the project path and installs the root package in
    editable mode, so a correctly bootstrapped checkout imports its own `src/`.
    Borrowing a different checkout's environment silently imports *that* checkout,
    and a green suite then says nothing about the branch under test. The failure is
    invisible in the test output, so it is worth one subprocess to rule out.
    """
    result = subprocess.run(
        [poetry, "run", "python", "-c", IMPORT_PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
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


def verify_command(poetry: str, env: dict[str, str] | None) -> str:
    """The gate command that works regardless of which virtualenv is activated.

    `poetry run python scripts/check.py` resolves the environment afresh, and in a shell
    whose `VIRTUAL_ENV` points elsewhere it resolves to the wrong one -- check.py then
    correctly refuses, and the obvious next move is to re-run this script, which changes
    nothing. Naming the interpreter directly ends that loop.
    """
    venv = poetry_query(poetry, ["env", "info", "--path"], env)
    if venv is None:
        return "poetry run python scripts/check.py"
    python = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return f"{python} scripts/check.py"


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
    env, note = install_environment(poetry)
    if note is not None:
        print(f"\n{note}", flush=True)

    try:
        run([poetry, "install"], cwd=ROOT, env=env)
        run([npm, "ci"], cwd=ROOT / "frontend")
        # Idempotent and about a second once the browser is in the user-level
        # cache, which the end-to-end step of `npm run check` drives.
        run([npx, "playwright", "install", "chromium"], cwd=ROOT / "frontend")
    except subprocess.CalledProcessError as exc:
        return exc.returncode

    failure = verify_environment(poetry, env)
    if failure is not None:
        print(failure, file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"\nBootstrapped {ROOT} in {elapsed:.0f}s.")
    print(f"Verify with: {verify_command(poetry, env)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
