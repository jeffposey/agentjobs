"""The durable controller against production dispatch, across real process deaths (task-416).

Every crash here is a real one: a child interpreter runs ``dispatch_task`` -- the
production guard chain, runner and journal -- and ``os._exit``s at one boundary of the
launch. The parent then builds a **fresh** ``Controller`` against the same home, journal
and task store, and nothing of the dead process's memory survives to help it. That is
design section 9a's harness requirement: restart a coordinator, do not reconstruct it.

The session driver is a fake CLI, and it is only evidence because its rows are shaped like
the real ones: ``claude agents --json --all`` on Claude Code 2.1.270 returned rows with
``id``, ``sessionId``, ``cwd``, ``kind``, ``name``, ``state`` and, when busy, ``status``, and
the ``name`` was the ``--name`` the dispatch launched with. The one capability the
controller relies on -- finding a launch by that name -- is that observed behaviour, and
``test_the_fake_listing_is_shaped_like_the_real_one`` holds the fixture to it. The one it
must not rely on -- absence proving a launch never happened -- the fake does not grant
either.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.controller import CAPABILITIES, Controller, capabilities_for
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import DispatchLedger, read_run
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import SESSION_NAME_PATTERN, DispatchRunner, runs_root
from agentjobs.execution import reducer
from agentjobs.execution.store import CONTROLLED_BY_CONTROLLER
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from skipping_clock import install
from support import task_store

TESTS = Path(__file__).resolve().parent
SOURCE = TESTS.parent / "src"

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

FAKE_CLAUDE = r"""
import json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
here = pathlib.Path(__file__).parent
ledger = here / "ledger.json"
rows = json.loads(ledger.read_text()) if ledger.exists() else []
argv = sys.argv[1:]

if argv and argv[0] == "agents":
    if (here / "listing.broken").exists():
        raise SystemExit(0)          # prints nothing: unreadable, not empty
    if "--all" not in argv:
        rows = [r for r in rows if r.get("state") != "stopped"]
    print(json.dumps(rows))
    raise SystemExit(0)
if argv and argv[0] == "logs":
    print("working")
    raise SystemExit(0)
if argv and argv[0] == "stop":
    for row in rows:
        if row["id"] == argv[1]:
            row["state"] = "stopped"
            row.pop("status", None)
    ledger.write_text(json.dumps(rows))
    print("stopped")
    raise SystemExit(0)

name = argv[argv.index("--name") + 1] if "--name" in argv else ""
short = "%08x" % (0xb55b0000 + len(rows))
rows.append({
    "id": short, "sessionId": short + "-0000-4000-8000-000000000000",
    "cwd": str(pathlib.Path.cwd()), "kind": "background", "name": name,
    "startedAt": 1787087345053, "status": "busy", "state": "working",
})
ledger.write_text(json.dumps(rows))
print("backgrounded \u00b7 " + short + " \u00b7 " + name)
"""

BATCH_WORKER = r"""
import pathlib, sys, time
release = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 120
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
"""

CRASHING_DISPATCH = r"""
import os, pathlib, sys
from datetime import datetime, timedelta
sys.path.insert(0, sys.argv[5])
from support import task_store
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
import agentjobs.dispatch.runner as runner_module
import agentjobs.clock as clock_module

home, task_id, point, caused_by = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
zero, offset = datetime.fromisoformat(sys.argv[6]), float(sys.argv[7])

class Frozen:
    # A separate process, so it inherits nothing of the harness's clock but these two
    # numbers. It has to read the same one: this child writes a run record the parent then
    # reads "how long ago" from, and two clocks there is the bug task-518 removes.
    def now(self):
        return zero + timedelta(seconds=offset)
    def monotonic(self):
        return offset
    def sleep(self, seconds):
        pass

clock_module.INSTALLED = Frozen()
project = ProjectRegistry(home=home).get("sandbox")
manager = TaskManager(task_store(project.root / "tasks", project_id="sandbox"))

def die(*args, **kwargs):
    sys.stdout.flush()
    os._exit(9)

if point == "before_marker":
    runner_module.deliver_identity = die
elif point == "after_marker":
    real_run = runner_module.subprocess.run
    def launch_then_nothing(argv, *args, **kwargs):
        if "--name" in argv:
            die()
        return real_run(argv, *args, **kwargs)
    runner_module.subprocess.run = launch_then_nothing
elif point == "after_launch":
    runner_module.DispatchRunner.capture_session_id = classmethod(lambda cls, stdout, reject=None: die())
elif point == "before_launched":
    runner_module.DispatchRunner._mark_launched = lambda self, *a, **k: die()
elif point == "batch_supervisor":
    runner_module.DispatchRunner._supervise_batch = lambda self, *a, **k: die()
elif point != "none":
    raise SystemExit("unknown point " + point)

handle = dispatch_task(
    manager=manager, project=project, project_config=project.load_config(),
    request=DispatchRequest(task_id=task_id, caused_by=caused_by),
    home=home, api_base="http://127.0.0.1:9",
)
if point == "batch_supervisor":
    handle.supervisor.join()
print(handle.run_id)
"""


class Clock:
    """This harness's face on the subsystem's one clock (task-518).

    It used to be real time plus an offset, and its own docstring said why: "dispatch
    reads the real clock, and the controller's view of how long ago has to agree with it
    until a test says otherwise". Those were two clocks, and every reading of the second
    one carried however long the machine had taken to get there. Now there is one, and
    ``advance`` is a wait the production code's own sleep would have made.

    Anchored on the wall clock rather than ahead of it, because task records here are
    stamped by the manager, which is not part of the dispatch subsystem and still reads
    real time. What matters is that there is exactly one origin, not where it is.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.skipping = install(monkeypatch, datetime.now(timezone.utc))

    def __call__(self) -> datetime:
        return self.skipping.now()

    def advance(self, seconds: float) -> None:
        dispatch_clock.sleep(seconds)


def environment(home: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SOURCE), str(TESTS), env.get("PYTHONPATH", "")])
    env["AGENTJOBS_HOME"] = str(home)
    env.pop("AGENTJOBS_RUN_ID", None)
    return env


class Machine:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.root = tmp_path / "project"
        (self.root / ".agentjobs").mkdir(parents=True)
        (self.root / "tasks").mkdir()
        self.home.mkdir()
        (self.root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
        )
        for command in (["init"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *command], cwd=self.root, capture_output=True)
        (self.root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.root, capture_output=True)
        self.cli_dir = tmp_path / "cli"
        self.cli_dir.mkdir()
        self.fake_cli = self.cli_dir / "claude.py"
        self.fake_cli.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.worker = tmp_path / "worker.py"
        self.worker.write_text(BATCH_WORKER, encoding="utf-8")
        self.release = tmp_path / "release"
        monkeypatch.setenv("AGENTJOBS_HOME", str(self.home))
        monkeypatch.delenv("AGENTJOBS_RUN_ID", raising=False)
        ProjectRegistry(home=self.home).add(self.root, project_id="sandbox")
        self.manager = TaskManager(task_store(self.root / "tasks", project_id="sandbox"))
        self.clock = Clock(monkeypatch)
        self.configure()

    def configure(self, *, mode: str = "session", controller: str = "active", **extra: Any) -> None:
        runner: Dict[str, object]
        if mode == "session":
            runner = {
                "mode": "session",
                "actor": "claude",
                "argv": [sys.executable, str(self.fake_cli), "--bg", "{prompt}"],
            }
        else:
            runner = {
                "mode": "batch",
                "actor": "claude",
                "argv": [sys.executable, str(self.worker), str(self.release), "{prompt}"],
            }
        project = {
            "enabled": True,
            "runner": "fake",
            "posture": extra.pop("posture", "auto"),
            "require_clean_tree": False,
            "resume_sessions": False,
        }
        project.update(extra.pop("project", {}))
        # A second runner is how a test says "a different credential" (task-463): the
        # credential a start would spend is read off the runner definition, so two of
        # them differing only in `env` are two subscriptions as far as the gate is
        # concerned.
        runners: Dict[str, object] = {"fake": runner}
        runners.update(extra.pop("runners", {}))
        config = {
            "version": 1,
            "enabled": True,
            "runners": runners,
            "projects": {"sandbox": project},
            "limits": {"max_concurrent_runs": 3, **extra.pop("limits", {})},
            "execution": {"controller": controller, **extra.pop("execution", {})},
        }
        (self.home / "dispatch.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    def task(self) -> str:
        created = self.manager.create_task(
            title="Recoverable",
            category="general",
            summary="A task to dispatch.",
            description="Do the thing.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        task = self.manager.add_log_entry(
            created.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go ahead."
        )
        self.authorised_by = task.log[-1].id
        return created.id

    def dispatch(self, task_id: str) -> Any:
        project = ProjectRegistry(home=self.home).get("sandbox")
        return dispatch_task(
            manager=self.manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(task_id=task_id, caused_by=self.authorised_by),
            home=self.home,
            api_base="http://127.0.0.1:9",
        )

    def crash(self, task_id: str, point: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                CRASHING_DISPATCH,
                str(self.home),
                task_id,
                point,
                str(self.authorised_by),
                str(TESTS),
                self.clock.skipping.origin.isoformat(),
                str(self.clock.skipping.elapsed),
            ],
            capture_output=True,
            text=True,
            env=environment(self.home),
            timeout=180,
        )

    def controller(self) -> Controller:
        """A fresh coordinator: nothing from any earlier controller object is reused."""
        return Controller(
            self.home,
            managers={"sandbox": self.manager},
            api_base="http://127.0.0.1:9",
        )

    def tick(self, times: int = 1) -> List[str]:
        lines: List[str] = []
        for _ in range(times):
            lines.extend(self.controller().tick().lines)
        return lines

    def rows(self) -> List[Dict[str, Any]]:
        path = self.cli_dir / "ledger.json"
        return json.loads(path.read_text()) if path.exists() else []

    def live_sessions(self) -> List[Dict[str, Any]]:
        return [row for row in self.rows() if row.get("state") != "stopped"]

    def execution(self, task_id: str) -> Any:
        store = journal(self.home)
        found = store.latest_execution("sandbox", task_id)
        assert found is not None
        return store.execution(found.execution_id)

    def attempts(self, task_id: str) -> List[Any]:
        return journal(self.home).attempts_for(self.execution(task_id).execution_id)

    def fire_retry(self) -> List[str]:
        """Let the controller schedule, move past any delay (the cap plus jitter), and tick."""
        lines = self.tick()
        self.clock.advance(700)
        return lines + self.tick(3)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    return Machine(tmp_path, monkeypatch)


def describe(completed: "subprocess.CompletedProcess[str]") -> str:
    """Everything the OS said about a child that did not exit the way a test expected.

    A bare ``died.stderr`` was the assertion message here for a year and it threw the
    evidence away: the recurring failure was a child that exited **1 with nothing on
    either stream**, and an empty message is exactly what that produces. That is not a
    Python error -- a traceback goes to stderr and an assertion has a message -- it is
    the signature of ``taskkill /F``, which is what happens when something on this
    machine aims a recycled pid at the wrong process (task-505).

    Naming that in the message is the difference between a red run that says what
    happened and one that costs an agent a cycle to reproduce.
    """
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    parts = [f"exit {completed.returncode}"]
    if completed.returncode == 1 and not stdout and not stderr:
        parts.append(
            "with empty stdout and stderr, which is what `taskkill /F` leaves behind: "
            "something killed this child rather than it failing (see task-505)"
        )
    parts.append(f"stdout: {stdout or '<empty>'}")
    parts.append(f"stderr: {stderr or '<empty>'}")
    return "\n".join(parts)


def wait_until_gone(pid: int, *, seconds: float = 60.0) -> None:
    """Block until nothing answers to this pid, so a test asserts on a settled machine."""
    from agentjobs.dispatch.pids import process_alive

    deadline = time.monotonic() + seconds
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def must_task(task: Optional[Any]) -> Any:
    assert task is not None
    return task


def _last_dispatch_result(manager: TaskManager, task_id: str) -> Optional[str]:
    task = manager.get_task(task_id)
    assert task is not None
    outcomes = [e.data.get("outcome") for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]
    return outcomes[-1] if outcomes else None


def task_of(name: Any) -> str:
    """The task id inside a dispatched session's name, read with the production grammar.

    Five test files used to recover it by hand -- ``rsplit("/", 1)[-1]``,
    ``endswith(f"/{task_id}")``, ``partition("#")[0]`` -- which is one grammar written
    five more times, and every copy broke together when task-500 put a slug in the name.
    There is one pattern in ``runner`` now, and this is what the suite reads it with.
    """
    match = SESSION_NAME_PATTERN.match(str(name or ""))
    assert match is not None, f"not a dispatched session name: {name!r}"
    return match.group("task")


# ----- the fixture is only evidence if it matches the real driver -------------------


def test_the_fake_listing_is_shaped_like_the_real_one(machine: Machine) -> None:
    task_id = machine.task()
    machine.dispatch(task_id)
    [row] = machine.rows()
    assert set(row) >= {"id", "sessionId", "cwd", "kind", "name", "state"}
    # No run stub since task-452, no project prefix and a slug from the title since
    # task-500: one live run of a task, in the only project holding one, is named for
    # the task and what it is about.
    assert row["name"] == f"{task_id} recoverable"


def test_capabilities_claim_nothing_the_drivers_do_not_offer() -> None:
    assert not any(c.authoritative_absence for c in CAPABILITIES.values())
    assert CAPABILITIES[("claude", "session")].correlation == "session_name"
    assert CAPABILITIES[("codex", "session")].correlation == "none"
    assert capabilities_for("claude", "batch").correlation == "worker_receipt"


# ----- routing: the two followers never share a run ---------------------------------


class TestRouting:
    def test_shadow_admissions_stay_with_the_poller(self, machine: Machine) -> None:
        machine.configure(controller="shadow")
        task_id = machine.task()
        machine.dispatch(task_id)
        execution = machine.execution(task_id)
        assert execution.controlled_by is None
        assert "retry_policy" not in execution.envelope
        assert machine.tick() == []

    def test_active_admissions_are_driven_by_the_controller_and_not_the_poller(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        handle = machine.dispatch(task_id)
        execution = machine.execution(task_id)
        assert execution.controlled_by == CONTROLLED_BY_CONTROLLER
        assert execution.envelope["retry_policy"] == dict(reducer.DEFAULT_RETRY_POLICY)
        polled = [r for r in poll_live_sessions(machine.home) if r.run_id == handle.run_id]
        assert polled == [], "the legacy poller leaves a controller-driven run alone"
        lines = machine.tick()
        assert any("observe" in line for line in lines)

    def test_startup_reconcile_leaves_a_controller_driven_run_to_the_controller(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        handle = machine.dispatch(task_id)
        results = DispatchLedger(machine.home, managers={"sandbox": machine.manager}).reconcile()
        mine = [r for r in results if r.run_id == handle.run_id]
        assert [r.detail for r in mine] == ["recovered by the durable controller"]
        assert journal(machine.home).attempt(handle.run_id).is_live  # type: ignore[union-attr]


# ----- crash windows around a launch (a1, a2) ------------------------------------------


def tick_inside_the_launch_window(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, *, after_seconds: float
) -> Tuple[str, List[str]]:
    """Dispatch a task, running a controller tick at the one moment it used to race.

    The tick runs as the runner is about to record the dispatch entry: the session exists
    and ``meta.yaml`` names it, and nothing on the task does yet. ``after_seconds`` is how
    long the launcher has apparently been stuck there when the tick lands.
    """
    original = DispatchRunner._record_dispatch
    lines: List[str] = []

    def ticking(self: DispatchRunner, *args: Any, **kwargs: Any) -> int:
        if after_seconds:
            machine.clock.advance(after_seconds)
        lines.extend(machine.tick())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DispatchRunner, "_record_dispatch", ticking)
    task_id = machine.task()
    machine.dispatch(task_id)
    return task_id, lines


class TestLaunchCrashWindows:
    def test_death_before_the_launcher_ran_is_retried_once_under_the_same_execution(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        died = machine.crash(task_id, "before_marker")
        assert died.returncode == 9, describe(died)
        assert machine.rows() == [], "nothing was launched"
        [first] = machine.attempts(task_id)
        assert first.state == "admitted"

        machine.tick()
        first = journal(machine.home).attempt(first.run_id)
        assert first is not None and not first.is_live and first.reservation == "refunded"
        assert machine.execution(task_id).state == "retry_wait"

        machine.fire_retry()
        attempts = machine.attempts(task_id)
        assert [a.run_id for a in attempts][0] == first.run_id and len(attempts) == 2
        assert len(machine.live_sessions()) == 1, "exactly one writer"
        assert attempts[1].execution_id == first.execution_id

    def test_death_after_the_launch_reattaches_through_the_listing_and_stops_nothing_twice(
        self, machine: Machine
    ) -> None:
        """The launcher printed an id nobody recorded; the name finds it."""
        task_id = machine.task()
        died = machine.crash(task_id, "after_launch")
        assert died.returncode == 9, describe(died)
        assert len(machine.live_sessions()) == 1
        [attempt] = machine.attempts(task_id)
        assert read_run(runs_root(machine.home) / attempt.run_id).session_id is None

        lines = machine.tick()
        # No dispatch entry reached the task, so nothing could ever follow it: stopped,
        # confirmed gone, and only then concluded.
        assert any("stopped unfollowable session" in line for line in lines), lines
        assert machine.live_sessions() == []
        assert not journal(machine.home).attempt(attempt.run_id).is_live  # type: ignore[union-attr]

        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 2
        assert len(machine.live_sessions()) == 1, "one replacement, never two"
        machine.fire_retry()
        assert len(machine.live_sessions()) == 1, "and replaying again launches nothing"

    def test_death_after_recording_the_session_reattaches_without_a_second_launch(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        died = machine.crash(task_id, "before_launched")
        assert died.returncode == 9, describe(died)
        [attempt] = machine.attempts(task_id)
        assert attempt.state == "admitted"

        lines = machine.tick(2)
        assert any("reattached session" in line for line in lines), lines
        attempt = journal(machine.home).attempt(attempt.run_id)
        assert attempt is not None and attempt.state == "live" and attempt.session_id
        assert len(machine.rows()) == 1, "no second session"
        assert machine.execution(task_id).state == "working"

    def test_a_tick_inside_a_live_launch_stops_nothing(
        self, machine: Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window between naming the session and recording the dispatch (task-522).

        The server's own poll runs beside the dispatches it serves, so a tick can land
        after the runner wrote ``session_id`` and before it wrote ``dispatch_entry_id``.
        That run is not unfollowable -- its launcher is alive and a moment from recording
        it -- and until task-522 the tick stopped it. Flake register entry 9 was this, seen
        through a fixture.
        """
        task_id, lines = tick_inside_the_launch_window(machine, monkeypatch, after_seconds=0)

        assert not any("stopped unfollowable" in line for line in lines), lines
        assert len(machine.live_sessions()) == 1
        machine.tick()
        [attempt] = machine.attempts(task_id)
        assert attempt.state == "live" and attempt.session_id

    def test_a_launcher_that_never_records_is_still_stopped_once_the_window_has_passed(
        self, machine: Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard above waits for a live launcher; it does not wait for ever."""
        _, lines = tick_inside_the_launch_window(machine, monkeypatch, after_seconds=121)

        assert any("stopped unfollowable session" in line for line in lines), lines

    def test_a_marked_launch_the_listing_cannot_find_is_unknown_not_absent(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        died = machine.crash(task_id, "after_marker")
        assert died.returncode == 9, describe(died)
        [attempt] = machine.attempts(task_id)

        machine.tick()  # inside the deadline: nothing is decided yet
        assert journal(machine.home).attempt(attempt.run_id).is_live  # type: ignore[union-attr]
        machine.clock.advance(601)
        lines = machine.tick(2)
        assert any("unknown" in line for line in lines), lines
        execution = machine.execution(task_id)
        assert execution.state != "retry_wait" and not execution.terminal
        still = journal(machine.home).attempt(attempt.run_id)
        assert still is not None and still.is_live, "ownership is kept while unknown"
        task = machine.manager.get_task(task_id)
        assert (
            task is not None
            and task.ball is Ball.HUMAN
            and "effect_unknown" in (task.ball_prompt or "")
        )
        machine.fire_retry()
        assert machine.rows() == [] and len(machine.attempts(task_id)) == 1, "nothing relaunched"

        # The one human action: Stop the run. It concludes, and no retry follows.
        DispatchLedger(machine.home, managers={"sandbox": machine.manager}).cancel(
            attempt.run_id, requester="Jeff Posey", source="gui"
        )
        machine.fire_retry()
        assert machine.execution(task_id).terminal
        assert len(machine.attempts(task_id)) == 1

    def test_a_fresh_process_performs_the_recovery(self, machine: Machine) -> None:
        """Nothing about the recovery lives in the test process."""
        task_id = machine.task()
        crashed = machine.crash(task_id, "before_marker")
        assert crashed.returncode == 9, describe(crashed)
        script = (
            "import pathlib, sys\n"
            "from agentjobs.dispatch.controller import Controller\n"
            "report = Controller(pathlib.Path(sys.argv[1]), api_base='http://127.0.0.1:9').tick()\n"
            "print('\\n'.join(report.lines))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", script, str(machine.home)],
            capture_output=True,
            text=True,
            env=environment(machine.home),
            timeout=180,
        )
        assert done.returncode == 0, describe(done)
        assert "never launched" in done.stdout
        assert machine.execution(task_id).state == "retry_wait"


# ----- observing: an unreadable listing concludes nothing (a2) ------------------------


def test_an_unreadable_listing_across_several_runs_concludes_none_of_them(
    machine: Machine,
) -> None:
    runs = [machine.dispatch(machine.task()).run_id for _ in range(2)]
    (machine.cli_dir / "listing.broken").write_text("", encoding="utf-8")
    machine.tick(2)
    poll_live_sessions(machine.home)
    store = journal(machine.home)
    assert all(store.attempt(run).is_live for run in runs)  # type: ignore[union-attr]
    for run in runs:
        assert read_run(runs_root(machine.home) / run).status == "running"


# ----- batch recovery proves quiescence and preserves work (a2) -------------------------


class TestBatchRecovery:
    def test_a_surviving_worker_is_left_alone_and_its_death_is_proved_not_guessed(
        self, machine: Machine
    ) -> None:
        machine.configure(mode="batch")
        task_id = machine.task()
        died = machine.crash(task_id, "batch_supervisor")
        assert died.returncode == 9, describe(died)
        [attempt] = machine.attempts(task_id)
        record = read_run(runs_root(machine.home) / attempt.run_id)
        assert record.pid is not None
        (machine.root / "half-done.txt").write_text("work in progress\n", encoding="utf-8")

        machine.tick(2)
        assert journal(machine.home).attempt(attempt.run_id).is_live  # type: ignore[union-attr]

        machine.release.write_text("go", encoding="utf-8")
        # Wait for what the assertion is about -- the controller concluding the attempt --
        # rather than for a pid and one tick after it (task-454). A tick that lands while
        # the worker still runs concludes nothing, which is the behaviour under test, so
        # ticking until the conclusion is not a retry of a flaky check: the controller
        # still has to prove the death itself. The bound is generous because the gate runs
        # this beside 25 other workers; a run that is never concluded still fails.
        lines: List[str] = []
        ticks = 0
        deadline = time.monotonic() + 60
        concluded = journal(machine.home).attempt(attempt.run_id)
        while concluded is not None and concluded.is_live and time.monotonic() < deadline:
            time.sleep(0.2)
            lines.extend(machine.tick())
            ticks += 1
            concluded = journal(machine.home).attempt(attempt.run_id)
        from agentjobs.dispatch.runner import RunDirectory

        receipt = RunDirectory(path=record.path).read_meta().get("pid_identity")
        worker = machine.controller().still_running(
            record.pid, identity=receipt if isinstance(receipt, str) else None
        )
        assert concluded is not None and not concluded.is_live, (
            f"not concluded after {ticks} ticks in 60s; the worker "
            f"{'is still running' if worker else 'is gone'}; lines: {lines}"
        )
        assert (machine.root / "half-done.txt").read_text(encoding="utf-8") == "work in progress\n"
        task = machine.manager.get_task(task_id)
        assert task is not None
        result = [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT][-1]
        assert "half-done.txt" in (result.body or "")
        assert machine.execution(task_id).state == "retry_wait"

    def test_a_reused_supervisor_pid_is_not_the_supervisor(self, machine: Machine) -> None:
        """The defect task-505 was filed about, and the twin of the test below it.

        ``_observe_batch`` returns early while the supervisor lives, and until task-505
        "lives" was ``alive(pid)`` -- a question about a number this machine recycles in
        seconds. A stranger holding the dead supervisor's number therefore read as still
        watching, so the run was never concluded, its slot never released and its retry
        never scheduled. ``alive`` answering yes for everything is that machine.
        """
        machine.configure(mode="batch")
        task_id = machine.task()
        crashed = machine.crash(task_id, "batch_supervisor")
        assert crashed.returncode == 9, describe(crashed)
        [attempt] = machine.attempts(task_id)
        record = read_run(runs_root(machine.home) / attempt.run_id)
        assert record.pid is not None
        machine.release.write_text("go", encoding="utf-8")
        wait_until_gone(record.pid)

        controller = machine.controller()
        controller.alive = lambda pid: True  # every number is answered by somebody
        lines = controller.tick().lines
        concluded = journal(machine.home).attempt(attempt.run_id)
        assert concluded is not None and not concluded.is_live, lines

    def test_a_live_stranger_at_the_supervisors_pid_concludes_the_run(
        self, machine: Machine
    ) -> None:
        """The same thing with a real process rather than a stubbed ``alive``.

        The stranger is started *after* the run was recorded, which is what proves it
        cannot be the supervisor -- the weaker of the two receipts, and the only one
        available for a run recorded before this fix.

        The run's ``started_at`` is moved back an hour rather than left where a test that
        takes three seconds puts it. `process_created_after` allows a second of slack for
        clock granularity, so a stranger spawned within that second of the record is
        genuinely indistinguishable from the supervisor and the check correctly declines
        to call it reuse -- which is a fact about the receipt, not about this fix, and is
        why a run records `supervisor_identity` as well. Left as written the test passed
        or failed on how fast the machine was, which is the whole class of thing this
        task exists to remove.
        """
        from agentjobs.dispatch.runner import RunDirectory

        machine.configure(mode="batch")
        task_id = machine.task()
        crashed = machine.crash(task_id, "batch_supervisor")
        assert crashed.returncode == 9, describe(crashed)
        [attempt] = machine.attempts(task_id)
        directory = RunDirectory(path=runs_root(machine.home) / attempt.run_id)
        record = read_run(directory.path)
        assert record.pid is not None
        machine.release.write_text("go", encoding="utf-8")
        wait_until_gone(record.pid)

        stranger = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time\nsys.stdout.write('up')\n"
                "sys.stdout.flush()\ntime.sleep(120)\n",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert stranger.stdout is not None
        stranger.stdout.read(2)
        try:
            assert record.started_at is not None
            directory.update_meta(
                supervisor_pid=stranger.pid,
                supervisor_identity=None,
                started_at=(record.started_at - timedelta(hours=1)).isoformat(),
            )
            lines = machine.tick(2)
            concluded = journal(machine.home).attempt(attempt.run_id)
            assert concluded is not None and not concluded.is_live, lines
        finally:
            stranger.kill()
            stranger.wait(timeout=30)

    def test_a_reused_pid_is_not_the_worker(self, machine: Machine) -> None:
        machine.configure(mode="batch")
        task_id = machine.task()
        crashed = machine.crash(task_id, "batch_supervisor")
        assert crashed.returncode == 9, describe(crashed)
        [attempt] = machine.attempts(task_id)
        record = read_run(runs_root(machine.home) / attempt.run_id)
        controller = machine.controller()
        controller.identity = lambda pid: "someone-else"
        controller.tick()
        concluded = journal(machine.home).attempt(attempt.run_id)
        assert concluded is not None and not concluded.is_live
        machine.release.write_text("go", encoding="utf-8")
        assert record.pid is not None


# ----- retries keep every limit (a3) ---------------------------------------------------


class TestRetriesKeepTheirLimits:
    def _worker_gone(self, machine: Machine, task_id: str) -> None:
        """The session vanishes from the listing: the poll concludes it worker_gone."""
        rows = machine.rows()
        for row in rows:
            row["state"] = "stopped"
            row.pop("status", None)
        (machine.cli_dir / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")
        machine.tick()

    def test_the_attempt_bound_escalates_once_with_the_saved_runner(self, machine: Machine) -> None:
        task_id = machine.task()
        machine.dispatch(task_id)
        for _ in range(2):
            self._worker_gone(machine, task_id)
            machine.fire_retry()
        assert len(machine.attempts(task_id)) == 3
        task = machine.manager.get_task(task_id)
        assert task is not None
        runners = {e.data.get("runner") for e in task.log if e.type is LogEntryType.DISPATCH}
        assert runners == {"fake"}
        self._worker_gone(machine, task_id)
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 3, "no fourth attempt"
        task = machine.manager.get_task(task_id)
        assert task is not None and task.ball is Ball.HUMAN
        assert "attempts_exhausted" in (task.ball_prompt or "")
        assert machine.execution(task_id).terminal

    def test_a_lifetime_cap_binds_a_retry_across_process_death(self, machine: Machine) -> None:
        machine.configure(limits={"auto": {"per_task_lifetime": 1}})
        task_id = machine.task()
        machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 1
        task = machine.manager.get_task(task_id)
        assert task is not None and "budget_exhausted" in (task.ball_prompt or "")

    def test_the_cooldown_makes_a_retry_wait_rather_than_refuses_it(self, machine: Machine) -> None:
        machine.configure(limits={"auto": {"cooldown_seconds": 3600}})
        task_id = machine.task()
        machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 1
        events = journal(machine.home).events(machine.execution(task_id).execution_id)
        assert [e.payload["class"] for e in events if e.kind == "policy_observed"] == ["cooldown"]
        assert not machine.execution(task_id).terminal, "a wait, not an escalation"
        machine.clock.advance(3600)
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 2

    def test_a_stop_between_attempts_survives_a_restart(self, machine: Machine) -> None:
        task_id = machine.task()
        handle = machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        assert machine.execution(task_id).state == "retry_wait"
        DispatchLedger(machine.home, managers={"sandbox": machine.manager}).cancel(
            handle.run_id, requester="Jeff Posey", source="gui"
        )
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 1
        assert machine.execution(task_id).terminal

    def test_disabled_launch_waits_without_spending_and_resumes_when_lifted(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        (machine.home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        for _ in range(3):
            machine.fire_retry()
        assert len(machine.attempts(task_id)) == 1
        events = journal(machine.home).events(machine.execution(task_id).execution_id)
        observed = [e.payload for e in events if e.kind == "policy_observed"]
        assert observed and all(o["class"] == "policy_wait" for o in observed)
        (machine.home / "DISPATCH_DISABLED").unlink()
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 2

    def test_a_lowered_ceiling_is_observed_and_clamps_the_retry(self, machine: Machine) -> None:
        machine.configure(posture="autonomous")
        task_id = machine.task()
        machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        machine.configure(posture="auto", project={"max_posture": "auto"})
        machine.fire_retry()
        assert len(machine.attempts(task_id)) == 2
        events = journal(machine.home).events(machine.execution(task_id).execution_id)
        observed = [e.payload for e in events if e.kind == "policy_observed"]
        assert observed[-1]["observed"]["posture_clamped_to"] == "auto"
        task = machine.manager.get_task(task_id)
        assert task is not None
        dispatches = [e for e in task.log if e.type is LogEntryType.DISPATCH]
        assert dispatches[-1].data["posture"] == "auto"
        assert dispatches[0].data["posture"] == "autonomous"

    def test_a_long_sleep_fires_one_retry_not_a_burst(self, machine: Machine) -> None:
        task_id = machine.task()
        machine.dispatch(task_id)
        self._worker_gone(machine, task_id)
        machine.tick()
        machine.clock.advance(12 * 3600)
        machine.tick(6)
        assert len(machine.attempts(task_id)) == 2


# ----- sessions AgentJobs did not start are admitted too (from-264-admission) -----------


class TestSessionsAreAdmitted:
    def test_an_interactive_claim_owns_its_task_in_the_journal_without_a_slot(
        self, machine: Machine
    ) -> None:
        from agentjobs.dispatch.interactive import start_interactive_run
        from agentjobs.session_identity import SessionIdentity

        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        project = ProjectRegistry(home=machine.home).get("sandbox")
        record = start_interactive_run(
            home=machine.home,
            project=project,
            task=must_task(machine.manager.get_task(task_id)),
            identity=SessionIdentity(session_id="11112222", cwd=str(machine.root), driver="claude"),
            actor="claude",
        )
        assert record is not None
        attempt = journal(machine.home).attempt(record.run_id)
        assert attempt is not None and attempt.is_live and not attempt.takes_slot
        assert attempt.session_id == "11112222"
        from agentjobs.dispatch.guards import LiveRunExistsError

        with pytest.raises(LiveRunExistsError):
            machine.dispatch(task_id)  # one live owner per task, decided by the journal

    def test_a_registered_session_cannot_free_itself_by_editing_its_meta(
        self, machine: Machine
    ) -> None:
        from agentjobs.dispatch.journal import admit_session, release_ended
        from agentjobs.dispatch.runner import RunDirectory

        task_id = machine.task()
        admit_session(
            machine.home,
            project_id="sandbox",
            task_id=task_id,
            run_id="run_registered",
            session_id="33334444",
            mode="session",
            takes_slot=True,
        )
        RunDirectory.create(
            machine.home,
            "run_registered",
            {
                "run_id": "run_registered",
                "task_id": task_id,
                "project_id": "sandbox",
                "mode": "session",
                "origin": "registered",
                "status": "finished",
                "session_id": "33334444",
            },
        )
        released = release_ended(machine.home, lambda _pid: machine.manager)
        assert released == []
        attempt = journal(machine.home).attempt("run_registered")
        assert attempt is not None and attempt.is_live and attempt.takes_slot
