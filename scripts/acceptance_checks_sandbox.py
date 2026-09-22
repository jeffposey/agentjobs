"""Stand up the task page so acceptance check results can be judged by hand, on its own port.

task-147 gave an acceptance criterion an executable ``check`` and gave the API a route
that runs one. task-152 is the half a person sees: each criterion's latest result on the
task page, and a **Run checks** button that produces one without leaving the record.

The tests cover the reading, the wiring, the refusal and the payload. What none of them
can answer is whether the line under a criterion **says what you wanted to know in the
second you looked at it** -- whether a failure reads as a failure, whether "not yet run"
is visibly a different thing from "failed", and whether pressing the button and watching
the page change feels like the checks ran or like the page reloaded. Those are looking
questions, so this is for looking.

**Both states are seeded, because the change makes the page behave differently in two
and comparing them is the review**:

    all passing       `task-001` has three criteria with checks and a recorded pass in
                      which every one exited 0. This is the state a finished branch is
                      in, and the one where the page should be quiet: green, terse, no
                      disclosure to open.

    one failing       `task-002` has the same shape and a recorded pass in which one
                      check exited 1 and one could not be started at all. **This is the
                      comparison.** Put the two tabs side by side: a reader skimming for
                      "is this done" must be able to tell them apart without reading a
                      word. Open the failure's Output and judge whether the tail is
                      enough to act on or merely enough to know something broke.

    never run         `task-003` carries checks and no pass has ever decided them. Worth
                      its own tab because "nobody has run this" and "this failed" are
                      the two states a status colour alone would collapse -- and a loop
                      that converged by breaking its own checks is what that collapse
                      costs.

    prose only        `task-004` has criteria and no checks. Nothing new should appear
                      on it at all: no button, no lines, no empty state. A task nobody
                      wrote a check for is not a task with three failing checks.

    dispatch off      `sandbox-checks-off` holds a copy of `task-001` on a project where
                      dispatch is disabled. Press Run checks there: the refusal must be
                      the **server's own sentence**, naming which gate said no. This is
                      the path a paraphrase would quietly ruin, and the one nobody looks
                      at until it happens to them.

Everything here is throwaway. Click anything, including the button that starts
processes -- the checks are short Python one-liners under a temporary directory, and
nothing touches the live corpus, the 8876 dashboard, or its registry. The data lives
under a temporary directory this process deletes when it stops, including its own
AGENTJOBS_HOME and its own dispatch.yaml.

    python scripts/acceptance_checks_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **At 390x844 first.** The check line is the newest thing on the longest page in this
    app, and a phone is where somebody reads a handoff. The sentence should not wrap
    into a second line at that width, and the Output disclosure should be thumb-sized.
  * **Press Run checks on `task-003`.** It should say it is running, then the three
    lines should change under you with no reload and no scroll jump. The two checks that
    exit 0 take about a second; the one that sleeps takes three, so you get to see the
    in-flight state rather than guess at it.
  * **Press it again on `task-002`.** The same pass runs again and the page should land
    in the same place. A second identical result that rearranged the list would mean the
    reading is order-dependent rather than keyed on the criterion.
  * **Read the failure sentence out loud.** `Check failed in 800ms — exited 1` against
    `Check failed in 5.0s — it could not be started`. Both are failures and only one is
    the branch's fault; say if the page makes that distinction hard to see.
  * **Look for a command.** There is deliberately no argv anywhere on the page and no
    field that would write one -- a browser that can write a check is a browser that can
    run anything on this machine. If you can find a way to type one, that is the defect.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from agentjobs.models_v2 import CheckOutcome

DEFAULT_PORT = 8913

PASSING = "sandbox-checks"
REFUSING = "sandbox-checks-off"

NAMES = {
    PASSING: "Sandbox: acceptance check results",
    REFUSING: "Sandbox: checks with dispatch off",
}

RUNNER = "sandbox-runner"


def quick(script: str) -> list[str]:
    """A check that is a real process and costs nothing: this interpreter, one -c."""
    return [sys.executable, "-c", script]


#: The three checks every seeded task carries. Two are quick and one sleeps, so the
#: in-flight state of the button is something you watch rather than something you are
#: told about.
GREEN = quick("print('42 passed in 0.41s')")
SLOW = quick("import time; time.sleep(3); print('e2e: 6 passed')")
RED = quick("import sys; print('src/app.py:12:1: F401 imported but unused'); sys.exit(1)")


def criteria(*, failing: bool, decided: bool = True) -> list[dict[str, object]]:
    """Three criteria with checks and one without, in one of the three states.

    ``decided`` is false for the task nothing has run, and it has to move the statuses
    as well as leave the log empty: a criterion reading ``met`` above ``Check not yet
    run`` is not a state the application can produce -- ``record_check_result`` writes
    both in one mutation -- so a fixture that produced it would be sending a reviewer
    to judge a contradiction this change cannot cause.
    """
    if not decided:
        pending = "pending"
        met = failed = pending
    else:
        met, failed, pending = "met", "failed", "pending"
    return [
        {
            "id": "ac-1",
            "text": "The unit suite is green.",
            "check": GREEN,
            "status": failed if failing else met,
        },
        {
            "id": "ac-2",
            "text": "The browser suite is green.",
            "check": SLOW,
            "status": met,
        },
        {
            "id": "ac-3",
            "text": "The linter is clean.",
            "check": RED if failing else GREEN,
            "status": failed if failing else met,
        },
        {
            "id": "ac-4",
            "text": "The check line reads clearly on a phone.",
            "status": pending,
        },
    ]


def passing_results() -> list["CheckOutcome"]:
    """A recorded pass in which every check exited 0."""
    from agentjobs.models_v2 import CheckOutcome

    return [CheckOutcome.model_validate(raw) for raw in [
        {"id": "ac-1", "status": "met", "exit_code": 0, "duration_seconds": 41.2},
        {"id": "ac-2", "status": "met", "exit_code": 0, "duration_seconds": 128.0},
        {"id": "ac-3", "status": "met", "exit_code": 0, "duration_seconds": 0.83},
    ]]


def failing_results() -> list["CheckOutcome"]:
    """A recorded pass with both kinds of failure in it, which is the point of it.

    One check exited non-zero, which is the branch's problem. One never started at all,
    which is the machine's. They render as different sentences because they call for
    different actions, and a page that collapsed them into one red word would be sending
    somebody to read a diff that is fine.
    """
    from agentjobs.models_v2 import CheckOutcome

    return [CheckOutcome.model_validate(raw) for raw in [
        {
            "id": "ac-1",
            "status": "failed",
            "exit_code": 1,
            "duration_seconds": 0.8,
            "output_tail": (
                "FAILED tests/test_loop.py::test_converges - AssertionError: "
                "expected 3 iterations, got 7\n"
                "=========================== short test summary ===========================\n"
                "1 failed, 214 passed in 0.79s"
            ),
        },
        {"id": "ac-2", "status": "met", "exit_code": 0, "duration_seconds": 126.4},
        {
            "id": "ac-3",
            "status": "failed",
            "exit_code": None,
            "duration_seconds": 5.0,
            "cause": "not_started",
            "output_tail": "FileNotFoundError: [WinError 2] The system cannot find the file specified",
        },
    ]]


def seed(manager, *, failing_copy: bool) -> None:
    """Four tasks: a green one, a red one, one nothing has run, and one with no checks."""
    from agentjobs.models_v2 import Lifecycle, Priority

    manager.create_task(
        id="task-001",
        title="Bound the loop driver's iteration count",
        summary="A chain that never converges currently runs until somebody notices.",
        description=(
            "Every check on this task has passed. It is the quiet state: read it beside "
            "task-002 and judge whether the difference is visible before you read a word."
        ),
        priority=Priority.HIGH,
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        category="reliability",
        acceptance=criteria(failing=False),
    )
    manager.record_check_result(
        "task-001",
        actor="Jeff Posey",
        results=passing_results(),
        unchecked=["ac-4"],
    )

    if failing_copy:
        # The dispatch-off project holds only the green copy: what is under review
        # there is the refusal, and a second task would only be a second way to it.
        return

    manager.create_task(
        id="task-002",
        title="Attachment thumbnails are served at full size",
        summary="A 4MB screenshot is sent whole to draw a 64px thumbnail.",
        description=(
            "Two of this task's checks failed, and they failed differently: one exited "
            "non-zero, one never started. Open the Output on each."
        ),
        priority=Priority.HIGH,
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        category="performance",
        acceptance=criteria(failing=True),
    )
    manager.record_check_result(
        "task-002",
        actor="Jeff Posey",
        results=failing_results(),
        unchecked=["ac-4"],
    )

    manager.create_task(
        id="task-003",
        title="Webhook retries have no ceiling",
        summary="Nothing has ever run this task's checks. Press the button and watch.",
        description=(
            "Three checks and no pass. `ac-2` sleeps for three seconds, so the button's "
            "in-flight state is something you see rather than something you are told."
        ),
        priority=Priority.MEDIUM,
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        category="reliability",
        acceptance=criteria(failing=False, decided=False),
    )

    manager.create_task(
        id="task-004",
        title="The dependency graph is unreadable past twenty nodes",
        summary="Every criterion here is prose. Nothing new should appear on this page.",
        description=(
            "No criterion carries a check, so there is no button and no check line. An "
            "empty state here would be the page inventing a machine verdict for a "
            "criterion a person was always going to decide."
        ),
        priority=Priority.LOW,
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        category="ux",
        acceptance=[
            {"id": "ac-1", "text": "The graph is legible at fifty nodes.", "status": "pending"},
            {"id": "ac-2", "text": "It reads on a phone.", "status": "pending"},
        ],
    )


def build(root: Path, *, project_id: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name=NAMES[project_id], user="Jeff Posey"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))
    seed(manager, failing_copy=project_id == REFUSING)
    return project_root


def write_dispatch_config(home: Path) -> None:
    """Two projects on one machine, differing only in whether dispatch is on.

    The check route is gated by `assert_dispatch_permitted` -- running a check starts a
    process of the record's choosing, which is the act the dispatch gates bound -- so
    "dispatch off" is the refusal a reader will actually meet, and it needs a project to
    happen on.
    """
    config = {
        "version": 1,
        "enabled": True,
        "runners": {RUNNER: {"argv": [sys.executable, "-c", "pass"], "actor": "claude"}},
        "projects": {
            PASSING: {"enabled": True, "runner": RUNNER, "posture": "auto", "require_clean_tree": False},
            REFUSING: {"enabled": False, "runner": RUNNER, "posture": "auto"},
        },
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def serve(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="agentjobs-acceptance-checks-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    for project_id in (PASSING, REFUSING):
        registry.add(build(root, project_id=project_id), project_id=project_id, name=NAMES[project_id])
    write_dispatch_config(home)

    base = f"http://127.0.0.1:{port}/app/p/{PASSING}/tasks"
    print(f"[review] acceptance-checks sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   all passing     {base}/task-001", flush=True)
    print(f"[review]   one failing     {base}/task-002", flush=True)
    print(f"[review]   never run       {base}/task-003", flush=True)
    print(f"[review]   prose only      {base}/task-004", flush=True)
    print(
        f"[review]   dispatch off    http://127.0.0.1:{port}/app/p/{REFUSING}/tasks/task-001",
        flush=True,
    )
    print(f"[review] data under {root}; Ctrl-C stops it and deletes everything.", flush=True)

    import uvicorn

    from agentjobs.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    serve(int(sys.argv[1]) if sys.argv[1:] else DEFAULT_PORT)


if __name__ == "__main__":
    main()
