"""Run the repository's Python and frontend verification gates."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

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


PYTHON = "python"
NPM = "npm"


@dataclass(frozen=True)
class Stage:
    """One check the gate runs, named so it can be asked for on its own."""

    name: str
    args: tuple[str, ...]
    cwd: Path
    what: str

    def command(self, npm: str) -> list[str]:
        """The argv to run, with the two runners resolved at call time.

        `sys.executable` and the npm shim are both properties of the machine rather
        than of the stage, so the table below stays a plain description of *what* runs
        and this decides *how*.
        """
        if self.args[0] == PYTHON:
            return [sys.executable, *self.args[1:]]
        return [npm, *self.args[1:]]


def stages() -> list[Stage]:
    """Every check, cheapest first, subject to the dependencies that are real.

    The ordering rule is one sentence: **a stage that can answer in seconds runs before
    one that takes minutes.** ENGINEERING.md already made that argument for Black, Ruff
    and MyPy and then stopped, leaving the frontend's own second-long hygiene checks --
    the OpenAPI match, the generated client, the icons, oxlint -- stranded behind a
    four-minute pytest run. Task-189 measured what that cost: four full gate runs, about
    sixteen minutes, to surface three failures, two of which were knowable in a second.

    Two orderings survive the reshuffle because they are genuine dependencies rather
    than habit: `build` writes the bundle that `e2e` then drives, and `api` exports the
    OpenAPI document before anything compares a generated client against it. Every other
    stage is independent, so its position is purely a question of what it costs.

    `pytest` sits where the cheap block ends, and the last three follow it in ascending
    order. Vitest and the build are seconds rather than minutes and could in principle
    join the cheap block; they are not there because both depend on the frontend
    toolchain being installed and neither has ever caught something the Python suite
    would have hidden. If that changes, move them -- the cost of each is printed at the
    end of every run, which is the whole point of printing it.
    """
    return [
        Stage("black", (PYTHON, "-m", "black", "--check", "."), ROOT, "Python formatting"),
        Stage("ruff", (PYTHON, "-m", "ruff", "check", "."), ROOT, "Python lint"),
        Stage("mypy", (PYTHON, "-m", "mypy", "."), ROOT, "Python types"),
        Stage("api", (NPM, "run", "check:api"), FRONTEND, "OpenAPI document and generated client"),
        Stage("icons", (NPM, "run", "check:icons"), FRONTEND, "generated PWA icons"),
        Stage("oxlint", (NPM, "run", "lint"), FRONTEND, "frontend lint"),
        Stage("pytest", (PYTHON, "-m", "pytest"), ROOT, "Python test suite"),
        Stage("vitest", (NPM, "run", "test"), FRONTEND, "frontend component suite"),
        Stage("build", (NPM, "run", "build"), FRONTEND, "typecheck and production build"),
        Stage("e2e", (NPM, "run", "test:e2e"), FRONTEND, "Playwright, against a live server"),
    ]


def select(all_stages: list[Stage], only: list[str], start: str | None) -> list[Stage]:
    """Narrow the gate for an iteration, in the table's order whatever order was asked.

    Selection exists for the loop between a late failure and its fix, and for nothing
    else. The unqualified `scripts/check.py` still runs every stage, and that is the
    form the commit rule in ENGINEERING.md names -- a flag that could be mistaken for
    the gate would be a way to commit past it.

    Unknown names raise rather than being skipped. A typo that silently selects nothing
    would report a green gate that ran no checks, which is the worst outcome available
    here.
    """
    known = {stage.name: stage for stage in all_stages}
    for name in [*only, *([start] if start else [])]:
        if name not in known:
            raise ValueError(f"unknown stage {name!r}; known stages: {', '.join(known)}")

    if only:
        wanted = set(only)
        return [stage for stage in all_stages if stage.name in wanted]
    if start:
        index = [stage.name for stage in all_stages].index(start)
        return all_stages[index:]
    return all_stages


def format_timings(timings: list[tuple[str, float]]) -> str:
    """A per-stage cost table, printed by every run so nobody has to instrument one.

    The whole argument of task-189 is a measurement, and a measurement that needs a
    special invocation to obtain is one that stops being taken. Re-measuring the budget
    in ENGINEERING.md is now a matter of reading the bottom of any gate run.
    """
    width = max((len(name) for name, _ in timings), default=len("total"))
    lines = [f"  {name.ljust(width)}  {seconds:6.1f}s" for name, seconds in timings]
    total = sum(seconds for _, seconds in timings)
    lines.append(f"  {'total'.ljust(width)}  {total:6.1f}s")
    return "\n".join(lines)


def scope_note(selected: list[Stage], all_stages: list[Stage]) -> str:
    """Say, in both directions, how much of the gate this run is.

    The risk `--only` and `--from` introduce is not that a partial run is slow; it is
    that its green is indistinguishable from the gate's green, so an agent iterating
    with `--from e2e` reports "the gate passed" having run one stage of ten. So a
    partial run names every stage it skipped, and says outright that it is not the gate.

    A full run says so too. "Ran every stage" is the sentence somebody quotes, and it
    should only be printable by a run that did.
    """
    if len(selected) == len(all_stages):
        return f"Ran every stage ({len(all_stages)} of {len(all_stages)})."
    ran = ", ".join(stage.name for stage in selected)
    skipped = ", ".join(stage.name for stage in all_stages if stage not in selected)
    return (
        f"PARTIAL RUN: {len(selected)} of {len(all_stages)} stages. Ran {ran}.\n"
        f"Skipped {skipped}. This is not the gate -- run `scripts/check.py` with no "
        "arguments before committing."
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the repository's verification gate.",
        epilog=(
            "With no arguments every stage runs, and that is the form the commit rule "
            "refers to. --only and --from exist for iterating on a late failure."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="STAGE",
        help="run just these stages (repeatable, or comma-separated)",
    )
    parser.add_argument(
        "--from",
        dest="start",
        metavar="STAGE",
        help="run this stage and every stage after it",
    )
    parser.add_argument("--list", action="store_true", help="print the stages and exit")
    args = parser.parse_args(argv)
    args.only = [name for group in args.only for name in group.split(",") if name]
    if args.only and args.start:
        parser.error("--only and --from cannot be combined")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the gate, cheapest stage first, and say what each stage cost.

    Format, lint and types run before pytest: together they take a handful of seconds
    and they fail on things pytest will never notice, so paying four minutes to find a
    misformatted file is the wrong order. Task-189 carried that reasoning through the
    frontend's own second-long checks, which used to sit behind pytest; `stages()` has
    the order and the two dependencies that constrain it.

    They are in the gate rather than only in ENGINEERING.md's pre-commit list because
    that list is documentation of an intention and this is the thing anyone actually
    runs. Task-166 found `poetry run mypy .` aborting on a module-name collision before
    it checked a single file -- it had never type-checked a line of this repository --
    and a `black` drift on `main`, both of which had survived precisely because nothing
    enforced them.
    """
    args = parse_args(argv)
    try:
        selected = select(stages(), args.only, args.start)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list:
        for stage in stages():
            print(f"  {stage.name:<8} {stage.what}")
        return 0

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

    scope = scope_note(selected, stages())
    print(f"\n{scope}", flush=True)

    timings: list[tuple[str, float]] = []
    for stage in selected:
        started = time.perf_counter()
        try:
            run(stage.command(npm), cwd=stage.cwd)
        except subprocess.CalledProcessError as exc:
            timings.append((stage.name, time.perf_counter() - started))
            print(f"\nFailed at stage '{stage.name}'.", file=sys.stderr)
            if len(timings) > 1:
                print(
                    f"Fix it, then resume with `--from {stage.name}` instead of paying "
                    "for the stages above a second time.",
                    file=sys.stderr,
                )
            print(f"\n{format_timings(timings)}", flush=True)
            return exc.returncode
        timings.append((stage.name, time.perf_counter() - started))

    # Repeated after the stages, not only before them: the line before is thousands of
    # lines of pytest output away by now, and the last thing printed is what gets read.
    print(f"\n{format_timings(timings)}\n\n{scope}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
