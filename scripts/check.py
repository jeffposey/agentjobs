"""Run the repository's Python and frontend verification gates."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# The one setup problem an activated virtualenv can explain, and so the only one its
# remedy should be offered for.
FOREIGN_IMPORT = "outside this checkout"

# What tells a nested `poetry run` to use an already-activated environment rather than
# the one Poetry keys on the project path. Named the same way in `scripts/bootstrap.py`.
ACTIVATION_VARS = ("VIRTUAL_ENV", "POETRY_ACTIVE")


def same_environment(active: str) -> bool:
    """Is `active` the virtualenv this interpreter is already running in?"""
    return os.path.normcase(os.path.realpath(active)) == os.path.normcase(
        os.path.realpath(sys.prefix)
    )


def child_environment() -> dict[str, str]:
    """The environment every child of the gate runs in, with the ambient one disowned.

    The gate's children are not all Python. `npm run check` shells out to
    `poetry run python` for the OpenAPI schema and for the icons, and Playwright's
    `webServer` starts `poetry run python e2e/run_server.py`. Poetry prefers an
    **activated** virtualenv over the one it keys on the project path, so every one of
    those nested calls resolves to whatever `VIRTUAL_ENV` names -- and a dispatched
    agent on this machine inherits a shell whose `VIRTUAL_ENV` is the main clone's. The
    source-provenance guard then refuses the end-to-end server, correctly, about six
    minutes in, after Black, Ruff, MyPy, pytest, Vitest and the production build have
    all gone green (task-210).

    This process already knows the answer those children keep getting wrong:
    `setup_problems` has just proved that *this* interpreter imports *this* checkout. So
    the gate stops deferring to the shell and hands its children an environment in which
    the question cannot be asked. Doing it here rather than in `playwright.config.ts` is
    what makes it general -- it covers every nested `poetry run` in the gate, including
    the ones nobody has written yet.

    An activated virtualenv that *is* this interpreter's is left alone. It agrees with
    us, so there is nothing to correct, and removing it would break the plain
    `python -m venv .venv` checkout that `scripts/bootstrap.py` also declines to
    overrule: there, Poetry has no path-keyed environment to fall back to.

    `PYTHONHOME` goes unconditionally. It is almost never set deliberately, and an
    inherited one sends a child interpreter to another installation's standard library
    and fails naming nothing useful.
    """
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    active = env.get("VIRTUAL_ENV")
    if active and not same_environment(active):
        for name in ACTIVATION_VARS:
            env.pop(name, None)
    return env


def disowned_environment_note() -> str | None:
    """Say so when the gate ignores the shell, so a green run is not a silent one."""
    active = os.environ.get("VIRTUAL_ENV")
    if not active or same_environment(active):
        return None
    return (
        f"Ignoring the activated virtualenv {active}.\n"
        f"Every check below runs against {sys.prefix}, this checkout's own environment, "
        "including the nested `poetry run` calls the frontend and Playwright make."
    )


def run(command: list[str], *, cwd: Path) -> None:
    """Run one check and stop immediately when it fails.

    The environment is deliberately not a parameter: a `run()` call that can opt out of
    the scrub is a `run()` call that will eventually forget to opt in.
    """
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=child_environment())


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
        problems.append(f"agentjobs imports from {origin.parent}, {FOREIGN_IMPORT}")

    if not (root / "frontend" / "node_modules").is_dir():
        problems.append("frontend/node_modules is missing")

    return problems


def remedy(problems: list[str]) -> str:
    """What to actually do about a checkout that cannot verify itself.

    The generic advice -- bootstrap, then `poetry run python scripts/check.py` -- is a
    loop when the import came from another checkout and a virtualenv is activated:
    `poetry run` resolves to *that* environment every time, so the gate refuses again on
    source it was never pointed at, and a dispatched agent on this machine inherits
    exactly that shell. Name the interpreter instead, which no activation can redirect
    (task-194).

    This is about reaching the gate, not about running it. Once the gate is running it
    disowns a foreign `VIRTUAL_ENV` for everything it spawns (`child_environment`), so
    the advice below only has to get the reader as far as the right interpreter.

    Scoped to the import problem deliberately. A missing `node_modules` has nothing to do
    with `VIRTUAL_ENV`, and advice that blames the wrong thing is worse than none.
    """
    active = os.environ.get("VIRTUAL_ENV")
    if active and any(FOREIGN_IMPORT in problem for problem in problems):
        return (
            f"The virtualenv {active} is activated in this shell, and `poetry run`\n"
            "will keep choosing it whatever you install. Run `python scripts/bootstrap.py`\n"
            "and use the interpreter path it prints, rather than `poetry run`."
        )
    return "Run `python scripts/bootstrap.py`, then `poetry run python scripts/check.py`."


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
            f"This checkout cannot verify itself: {'; '.join(problems)}.\n" f"{remedy(problems)}",
            file=sys.stderr,
        )
        return 1

    note = disowned_environment_note()
    if note is not None:
        print(f"\n{note}", flush=True)

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
