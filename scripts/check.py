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

# `gate_scope` lives beside this file rather than in the package on purpose: the gate has
# to be able to report that Black failed even when `agentjobs` itself will not import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# mypy resolves imports from the invocation directory, and `mypy_path = "scripts"` is not
# the fix: it makes every script visible under two module names and aborts the run on the
# collision, which is the failure task-166 spent a session on. `gate_scope` is checked on
# its own merits as a top-level module either way.
import gate_scope  # type: ignore[import-not-found] # noqa: E402

# The one setup problem an activated virtualenv can explain, and so the only one its
# remedy should be offered for.
FOREIGN_IMPORT = "outside this checkout"

# What tells a nested `poetry run` to use an already-activated environment rather than
# the one Poetry keys on the project path. Named the same way in `scripts/bootstrap.py`.
ACTIVATION_VARS = ("VIRTUAL_ENV", "POETRY_ACTIVE")

# What tells a process it is part of a dispatched run. The gate records its own phases
# and its stages must not: `pytest` runs this repository's own tests of `check.main`,
# which would then append a gate record per simulated run straight into the live ledger.
# Observed the first time the gate was run inside a real run directory -- sixteen phantom
# records beside one true one. Named the same way in `agentjobs.dispatch.phases`.
RUN_VARS = ("AGENTJOBS_RUN_ID", "AGENTJOBS_RUN_DIR")


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

    So does the dispatched-run pair, for a different reason: this process records the
    gate's own phases, and a stage that inherited them would record more. `pytest` is the
    case that bites -- it runs this repository's tests of `check.main`, each of which
    would append a gate record to the live run. See `RUN_VARS`.
    """
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    for name in RUN_VARS:
        env.pop(name, None)
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

PARALLEL_ARGS = ("-n", "auto")
"""Run the Python suite across every core, via pytest-xdist.

The suite is 2538 tests and 89% of the gate. It is also, as of task-233, parallel-safe:
`tests/conftest.py` gives every test its own project registry, its own Claude home and a
stubbed reachability probe, and nothing in it binds a fixed port -- the four places that
open a socket ask the kernel for port 0. Measured on this 32-core machine, same commit,
same 2538 passing: 494s serial with coverage, 431s serial without, **43s at `-n auto`**.

Parallelism lives here rather than in `pyproject.toml`'s `addopts` so that the *gate* is
parallel while a hand-run `pytest -k something` stays serial. That is the right split in
both directions: xdist costs more than it saves on a handful of tests, and its
interleaved output is worse to read when you are debugging one.

`--serial` turns it off for the case where the interleaving is the problem.
"""

COVERAGE_ARGS = ("--cov=src/agentjobs", "--cov-report=term-missing", "--cov-report=html")
"""Coverage, which the gate no longer pays for on every run.

These three flags used to be in `addopts`, so every invocation of pytest anywhere --
gate, one test, a `-k` selection -- instrumented the whole package and wrote an HTML
report. Measured cost on the full suite: **63 seconds, 13% of the pytest stage**, and
nothing reads `htmlcov/` before a commit. The gate's job is to catch what is broken, and
a coverage number has never been what caught it.

Coverage is still a thing this repository cares about; it is now something you ask for.
`scripts/check.py --coverage`, or `pytest --cov=src/agentjobs` directly.
"""


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


def stages(*, coverage: bool = False, parallel: bool = True) -> list[Stage]:
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

    The pytest stage is the only one with options, because it is the only one that costs
    minutes. See `PARALLEL_ARGS` and `COVERAGE_ARGS`.
    """
    pytest_args = [PYTHON, "-m", "pytest"]
    if parallel:
        pytest_args.extend(PARALLEL_ARGS)
    if coverage:
        pytest_args.extend(COVERAGE_ARGS)
    return [
        Stage("black", (PYTHON, "-m", "black", "--check", "."), ROOT, "Python formatting"),
        Stage("ruff", (PYTHON, "-m", "ruff", "check", "."), ROOT, "Python lint"),
        Stage("mypy", (PYTHON, "-m", "mypy", "."), ROOT, "Python types"),
        Stage("api", (NPM, "run", "check:api"), FRONTEND, "OpenAPI document and generated client"),
        Stage("icons", (NPM, "run", "check:icons"), FRONTEND, "generated PWA icons"),
        Stage("oxlint", (NPM, "run", "lint"), FRONTEND, "frontend lint"),
        Stage("pytest", tuple(pytest_args), ROOT, "Python test suite"),
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


def record_phase(kind: str, **fields: object) -> None:
    """Tell the run ledger what the gate just did, when this gate is inside a run.

    A dispatched run knew when it started and when it stopped and nothing in between;
    the only other artefact was a TTY capture from which no phase attribution survives
    (task-233). The gate is the largest single phase of a run and the one that can
    report itself exactly, so it does.

    Every failure is swallowed, including the import. Instrumentation that can break the
    thing it measures is worse than no instrumentation, and outside a dispatched run
    `record_phase_from_env` writes nothing anyway.
    """
    try:
        from agentjobs.dispatch.phases import record_phase_from_env

        record_phase_from_env(kind, **fields)
    except Exception:  # noqa: BLE001 - see the docstring; never fail the gate over this
        return


def issue_receipt(basis: str | None) -> str:
    """Attest that this checkout's gate is satisfied at HEAD, and say what happened.

    Only ever called after a green run of every stage a run was entitled to skip nothing
    from. The receipt is what makes `--since-gate` possible later: without a commit the
    gate itself verified, a reduced run would be resting on somebody's judgement, which
    is the thing task-221 said not to build.

    A dirty tree gets no receipt. The receipt names a commit, and the tree that just
    passed is not that commit if anything is uncommitted.
    """
    commit = gate_scope.head_commit(ROOT)
    if commit is None:
        return "No gate receipt written: this is not a git checkout."
    if not gate_scope.tree_is_clean(ROOT):
        return (
            "No gate receipt written: the working tree is dirty, so there is no commit "
            "this green run attests to. Commit, then run the gate again to earn one."
        )
    if gate_scope.write_receipt(ROOT, commit, basis=basis) is None:
        return "No gate receipt written: the git directory is not writable."
    derived = f", derived from {basis[:8]}" if basis else ""
    return f"Gate receipt written for {commit[:8]}{derived}."


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
    parser.add_argument(
        "--since-gate",
        action="store_true",
        help="run only the stages the changes since the last verified commit can affect",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="measure coverage during pytest and write htmlcov/ (off by default)",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="run pytest in one process, for readable output while debugging a failure",
    )
    parser.add_argument("--list", action="store_true", help="print the stages and exit")
    args = parser.parse_args(argv)
    args.only = [name for group in args.only for name in group.split(",") if name]
    if args.only and args.start:
        parser.error("--only and --from cannot be combined")
    if args.since_gate and (args.only or args.start):
        parser.error("--since-gate selects stages itself; it cannot be combined with --only/--from")
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
    all_stages = stages(coverage=args.coverage, parallel=not args.serial)
    names = [stage.name for stage in all_stages]

    scope_result = gate_scope.resolve(ROOT, names) if args.since_gate else None
    try:
        if scope_result is not None and scope_result.reduced:
            wanted = set(scope_result.stages or [])
            selected = [stage for stage in all_stages if stage.name in wanted]
        else:
            selected = select(all_stages, args.only, args.start)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list:
        for stage in all_stages:
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

    if scope_result is None:
        scope = scope_note(selected, all_stages)
        kind = "full" if len(selected) == len(all_stages) else "partial"
    else:
        scope = gate_scope.render(scope_result, names)
        kind = "necessity" if scope_result.reduced else "full"
    print(f"\n{scope}", flush=True)

    if not selected:
        # Only reachable from --since-gate with an unchanged tree. There is nothing to
        # run and nothing new to attest to, so the existing receipt stands.
        return 0

    record_phase(
        "gate_started",
        scope=kind,
        stages=[stage.name for stage in selected],
        stages_total=len(all_stages),
    )
    began = time.perf_counter()

    timings: list[tuple[str, float]] = []
    for stage in selected:
        started = time.perf_counter()
        try:
            run(stage.command(npm), cwd=stage.cwd)
        except subprocess.CalledProcessError as exc:
            timings.append((stage.name, time.perf_counter() - started))
            record_phase(
                "gate_finished",
                scope=kind,
                passed=False,
                seconds=round(time.perf_counter() - began, 1),
                stages_run=len(timings),
                stages_total=len(all_stages),
                failed_stage=stage.name,
            )
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

    record_phase(
        "gate_finished",
        scope=kind,
        passed=True,
        seconds=round(time.perf_counter() - began, 1),
        stages_run=len(timings),
        stages_total=len(all_stages),
    )

    # A receipt is earned by a run that skipped nothing it was not entitled to skip: a
    # full run, or a --since-gate run whose skips were derived from an earlier receipt.
    # An --only/--from run never earns one, which is the same rule PARTIAL RUN states.
    receipt = ""
    if kind == "full":
        receipt = f"\n{issue_receipt(None)}"
    elif kind == "necessity":
        receipt = f"\n{issue_receipt(scope_result.commit if scope_result else None)}"

    # Repeated after the stages, not only before them: the line before is thousands of
    # lines of pytest output away by now, and the last thing printed is what gets read.
    print(f"\n{format_timings(timings)}\n\n{scope}{receipt}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
