"""Durable dispatch, proved end to end against the incidents that motivated it (task-419).

Design section 9a of ``docs/agent-dispatch-design.md`` asks for one harness that drives the
production reducer and adapters -- dispatch, the controller, the poller's session follower,
auth recovery, the finisher -- against recorded failure timelines, with real process deaths
and a fresh coordinator after each. This is that harness. ``README.md`` beside it holds the
fixture provenance and the capability and guarantee matrix it proves.

**What is real and what is simulated.** Real: the task store, the execution journal, git
repositories, every production module named above, and child interpreters that die with
``os._exit``. Simulated: the Claude CLI (a script answering the same commands with rows
shaped like Claude Code 2.1.270's), the credential store behind it (a file saying whether a
probe is answered), the agent's own work (the test hands off or closes as the agent would)
and the clock. Isolated real-driver checks are ``TestTheRealDriverContract``, opt-in.

**Deterministic by construction** (task-414's owner direction, 2026-09-13). The clock is a
``FakeClock`` injected into every runner, the controller and auth recovery; nothing here
sleeps; a subprocess is either a fake CLI invocation that answers at once or a child that
dies at a named line. Two clocks cannot disagree under load, because the fake one starts two
hours ahead of the wall clock and only moves when a test moves it.

**Human actions are counted, and so is delivery.** ``Person`` performs an action only when
the task record asks for it, and records it. A scenario asserts the count *and* that the run
reached its terminal state or the one wait the design justifies -- a harness that did
nothing would otherwise score a perfect zero.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

from agentjobs.dispatch import auth_recovery
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.controller import Controller
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import DispatchLedger, find_run
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import DispatchRunner, RunDirectory, runs_root
from agentjobs.execution.factory import close_execution_stores
from agentjobs.manager import TaskManager
from agentjobs.dispatch.config import Posture
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from support import task_store

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
TESTS = HERE.parent
SOURCE = TESTS.parent / "src"

PERSON = "Jeff Posey"
AGENT = "claude"
LOGIN_EXPIRED = "Login expired · Please run /login"


def load_fixture(name: str) -> Dict[str, Any]:
    loaded = yaml.safe_load((FIXTURES / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ----- the fake driver --------------------------------------------------------------

FAKE_CLAUDE = r"""
import json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8")
here = pathlib.Path(__file__).parent
ledger = here / "ledger.json"
argv = sys.argv[1:]

def rows():
    return json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else []

def save(value):
    ledger.write_text(json.dumps(value), encoding="utf-8")

def log(name, value):
    with (here / name).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + chr(10))

if argv[:1] == ["agents"]:
    listed = rows() if "--all" in argv else [r for r in rows() if r.get("state") != "stopped"]
    print(json.dumps(listed))
    raise SystemExit(0)

if argv[:1] == ["logs"]:
    print("session output")
    raise SystemExit(0)

if argv[:1] == ["stop"]:
    log("calls.log", argv)
    updated = []
    for row in rows():
        if row["id"] == argv[1]:
            row.update({"pid": None, "status": "stopped", "state": "stopped"})
        updated.append(row)
    save(updated)
    print("stopped")
    raise SystemExit(0)

if argv[:1] == ["-p"]:
    sys.stdin.read()
    store = json.loads((here / "store.json").read_text(encoding="utf-8"))
    log("probes.log", {"argv": argv, "store": store})
    if store["answer"] == "ready":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "api_error_status": None, "result": "AUTH_OK", "num_turns": 1}))
        raise SystemExit(0)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "api_error_status": store.get("status", 401), "result": store["text"]}))
    raise SystemExit(store.get("exit", 1))

if argv[:2] == ["--bg", "--resume"]:
    message = sys.stdin.read()
    log("nudges.log", {"argv": argv, "stdin": message})
    updated = []
    for row in rows():
        if row.get("sessionId") == argv[2]:
            row.update({"pid": 5151, "status": "busy", "state": "working"})
        updated.append(row)
    save(updated)
    reply = here / "reply_on_wake.json"
    if reply.is_file():
        plan = json.loads(reply.read_text(encoding="utf-8"))
        target = plan["transcripts"].get(argv[2])
        if target:
            with open(target, "a", encoding="utf-8") as handle:
                for line in plan["lines"][argv[2]]:
                    handle.write(json.dumps(line) + chr(10))
    print("woke session " + argv[2][:8] + " with its saved options (--model)")
    raise SystemExit(0)

current = rows()
number = len(current)
short = "%08x" % (0xa19e0000 + number)
full = short + "-0000-4000-8000-%012d" % number
name = argv[argv.index("--name") + 1] if "--name" in argv else ""
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
log("launches.log", {"id": short, "name": name, "model": model})
current.append({
    "id": short, "sessionId": full, "cwd": str(pathlib.Path.cwd()), "kind": "background",
    "name": name, "pid": 4000 + number, "startedAt": 1787087345053, "status": "busy",
    "state": "working",
})
save(current)
print("backgrounded · " + short + " · " + name)
"""


class FakeClock:
    """The only clock the code under test reads, and only the test moves it.

    Two hours ahead of the wall clock, so anything that did stamp real time -- a task log
    entry, a journal admission -- is always in this clock's past however slowly a loaded
    machine runs the test. Offsets are seconds since ``zero``, which a scenario sets at its
    fixture's ``at: 0``.
    """

    def __init__(self) -> None:
        self.zero = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        self.offset = 0.0

    def __call__(self) -> datetime:
        return self.zero + timedelta(seconds=self.offset)

    def at(self, seconds: float) -> datetime:
        return self.zero + timedelta(seconds=seconds)

    def move_to(self, seconds: float) -> None:
        assert seconds >= self.offset, "a fixture clock never runs backwards"
        self.offset = seconds


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# ----- who touched what ---------------------------------------------------------------


@dataclass
class Person:
    """The human, reduced to what the harness must count: actions the record asked for.

    ``act`` refuses an action the task is not asking for, so an unsolicited click can never
    be counted as a required one -- and a required one can never be skipped silently,
    because a scenario asserts the run reached its end.
    """

    actions: List[Tuple[str, str]] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)

    def act(self, kind: str, task: Any, *, because: str) -> None:
        assert task.ball is Ball.HUMAN, f"{kind} was not asked for: the ball is {task.ball}"
        assert because in (task.ball_prompt or ""), (
            f"{kind} was not what the record asked for: {task.ball_prompt!r}"
        )
        self.actions.append((kind, task.id))

    def message(self, body: str) -> None:
        """Something a person chose to say. Not a required action, and not counted as one."""
        self.messages.append(body)


def pages(task: Any, *, since_entry: int = 0) -> List[Any]:
    """Machine-written handoffs that put the ball with a person: the notifications."""
    return [
        entry
        for entry in task.log
        if entry.id > since_entry
        and entry.type is LogEntryType.HANDOFF
        and entry.actor in ("dispatcher", "finisher")
        and entry.data.get("ball") == Ball.HUMAN.value
    ]


def dispatch_entries(task: Any) -> List[Dict[str, Any]]:
    return [dict(entry.data) for entry in task.log if entry.type is LogEntryType.DISPATCH]


# ----- the world ----------------------------------------------------------------------


PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [{"name": PERSON, "kind": "human"}, {"name": AGENT, "kind": "agent"}],
    "default_user": PERSON,
}


class World:
    """One machine: a home, projects with git roots, the fake CLI and a fake clock."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        projects: Tuple[str, ...] = ("sandbox",),
    ) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.claude_home = tmp_path / "claude"
        self.claude_home.mkdir()
        self.cli_dir = tmp_path / "cli"
        self.cli_dir.mkdir()
        self.cli = self.cli_dir / "claude.py"
        self.cli.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.store_answers("refused")
        self.clock = FakeClock()
        self.person = Person()
        self.managers: Dict[str, TaskManager] = {}
        self.roots: Dict[str, Path] = {}
        monkeypatch.setenv("AGENTJOBS_HOME", str(self.home))
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(self.claude_home))
        monkeypatch.delenv("AGENTJOBS_RUN_ID", raising=False)
        # Every runner the code under test builds -- the dispatcher's, the poller's, the
        # controller's, auth recovery's -- reads this clock and no other.
        defaults = dict(DispatchRunner.__init__.__kwdefaults__ or {})
        defaults["clock"] = self.clock
        monkeypatch.setattr(DispatchRunner.__init__, "__kwdefaults__", defaults)
        # The poller's own controller and recovery passes read the wall clock; the harness
        # drives both itself, on the fake one, so a poll only follows sessions.
        monkeypatch.setattr("agentjobs.dispatch.poller._recover_parked", lambda *a: [])
        monkeypatch.setattr("agentjobs.dispatch.poller._drive_controller", lambda *a: [])
        for project_id in projects:
            self.add_project(project_id)
        self.configure()

    # -- configuration -----------------------------------------------------------------

    def add_project(self, project_id: str) -> None:
        root = self.tmp / project_id
        (root / ".agentjobs").mkdir(parents=True)
        (root / "tasks").mkdir()
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
        )
        for command in (
            ["init", "--initial-branch=main"],
            ["config", "user.email", "t@t.t"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", *command], cwd=root, capture_output=True, check=True)
        (root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True
        )
        ProjectRegistry(home=self.home).add(root, project_id=project_id)
        self.roots[project_id] = root
        self.managers[project_id] = TaskManager(task_store(root / "tasks", project_id=project_id))

    def adopt(self, project_id: str, root: Path, manager: TaskManager) -> None:
        """A project something else built -- a clone with a branch in a worktree."""
        (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
        )
        (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", ".gitignore"], cwd=root, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: ignore machine-local config"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        self.roots[project_id] = root
        self.managers[project_id] = manager
        self.configure()

    def configure(
        self,
        *,
        group: str = "default",
        posture: str = "auto",
        controller: str = "active",
        fable_enabled: bool = True,
        limits: Optional[Dict[str, object]] = None,
    ) -> None:
        def runner(model: str) -> Dict[str, object]:
            return {
                "mode": "session",
                "actor": AGENT,
                "argv": [sys.executable, str(self.cli), "--bg", "--model", model, "{prompt}"],
            }

        config = {
            "version": 1,
            "enabled": True,
            "runners": {"fable": runner("claude-fable-5-1"), "opus": runner("claude-opus-5")},
            "runner_groups": {
                "default": {"members": ["opus"]},
                "big-dawg": {"members": [{"runner": "fable", "enabled": fable_enabled}, "opus"]},
            },
            "projects": {
                project_id: {
                    "enabled": True,
                    "group": group,
                    "posture": posture,
                    "max_posture": "autonomous",
                    "require_clean_tree": False,
                    "resume_sessions": False,
                }
                for project_id in self.roots
            },
            "limits": {
                "max_concurrent_runs": 4,
                "session_stale_seconds": 7 * 24 * 3600,
                "session_stall_seconds": 7 * 24 * 3600,
                **(limits or {}),
            },
            "execution": {"controller": controller},
        }
        (self.home / "dispatch.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )

    # -- the credential store and the session ------------------------------------------

    def store_answers(self, answer: str, *, text: str = LOGIN_EXPIRED, exit: int = 1) -> None:
        (self.cli_dir / "store.json").write_text(
            json.dumps({"answer": answer, "text": text, "exit": exit}), encoding="utf-8"
        )

    def rows(self) -> List[Dict[str, Any]]:
        path = self.cli_dir / "ledger.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def set_rows(self, rows: List[Dict[str, Any]]) -> None:
        (self.cli_dir / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")

    def row(self, run_id: str) -> Dict[str, Any]:
        [found] = [r for r in self.rows() if r["name"].endswith(run_id[len("run_") :])]
        return found

    def live_sessions(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            r
            for r in self.rows()
            if r.get("state") != "stopped" and (task_id is None or f"/{task_id}@" in r["name"])
        ]

    def calls(self, name: str) -> List[Any]:
        path = self.cli_dir / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def set_session(self, run_id: str, **fields: Any) -> None:
        target = self.row(run_id)["id"]
        self.set_rows([dict(r, **fields) if r["id"] == target else r for r in self.rows()])

    def transcript(self, run_id: str) -> Path:
        full = self.row(run_id)["sessionId"]
        directory = self.claude_home / "projects" / "C--sandbox"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{full}.jsonl"

    def write_line(self, run_id: str, line: Dict[str, Any]) -> None:
        with self.transcript(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")

    def stall(self, run_id: str, at: float, text: str = LOGIN_EXPIRED) -> None:
        """The session's turn ends on a synthetic auth failure, and it goes idle."""
        self.write_line(
            run_id,
            {
                "type": "assistant",
                "timestamp": stamp(self.clock.at(at)),
                "message": {
                    "model": "<synthetic>",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
                "error": "authentication_failed",
                "isApiErrorMessage": True,
                "sessionId": self.row(run_id)["sessionId"],
                "sessionKind": "bg",
            },
        )
        self.set_session(run_id, status="idle", state="done", pid=None)

    def reply_when_woken(self, run_id: str, at: float) -> None:
        """A woken session answers with a real model turn, stamped at fixture time ``at``."""
        full = self.row(run_id)["sessionId"]
        plan_path = self.cli_dir / "reply_on_wake.json"
        plan = (
            json.loads(plan_path.read_text(encoding="utf-8"))
            if plan_path.is_file()
            else {"transcripts": {}, "lines": {}}
        )
        plan["transcripts"][full] = str(self.transcript(run_id))
        plan["lines"][full] = [
            {
                "type": "user",
                "timestamp": stamp(self.clock.at(at)),
                "message": {"role": "user", "content": "resumed"},
                "sessionId": full,
            },
            {
                "type": "assistant",
                "timestamp": stamp(self.clock.at(at + 2)),
                "message": {
                    "model": "claude-opus-5",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Carrying on."}],
                },
                "sessionId": full,
            },
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

    # -- tasks and dispatch -------------------------------------------------------------

    def task(self, project_id: str = "sandbox", title: str = "Recoverable") -> str:
        manager = self.managers[project_id]
        created = manager.create_task(
            title=title,
            category="general",
            summary="A task to dispatch.",
            description="Do the thing.",
            lifecycle=Lifecycle.READY,
            actor=PERSON,
        )
        return created.id

    def authorise(self, task_id: str, project_id: str = "sandbox") -> int:
        task = self.managers[project_id].add_log_entry(
            task_id, actor=PERSON, type=LogEntryType.NOTE, body="Go ahead."
        )
        return int(task.log[-1].id)

    def dispatch(
        self,
        task_id: str,
        *,
        project_id: str = "sandbox",
        group: Optional[str] = None,
        posture: Optional[Posture] = None,
        request: Optional[DispatchRequest] = None,
    ) -> Any:
        project = ProjectRegistry(home=self.home).get(project_id)
        return dispatch_task(
            manager=self.managers[project_id],
            project=project,
            project_config=project.load_config(),
            request=request
            or DispatchRequest(
                task_id=task_id,
                caused_by=self.authorise(task_id, project_id),
                group=group,
                posture=posture,
            ),
            home=self.home,
            api_base="http://127.0.0.1:9",
            now=self.clock(),
        )

    def get(self, task_id: str, project_id: str = "sandbox") -> Any:
        task = self.managers[project_id].get_task(task_id)
        assert task is not None
        return task

    def meta(self, run_id: str) -> Dict[str, Any]:
        return dict(RunDirectory(path=runs_root(self.home) / run_id).read_meta())

    # -- one moment of the machine -------------------------------------------------------

    def fresh_stores(self) -> None:
        """A restarted coordinator: nothing cached in this process survives into the next."""
        close_execution_stores()

    def tick(self, at: float) -> List[str]:
        """Everything the server's poll tick does, at fixture time ``at``, from fresh objects."""
        self.clock.move_to(at)
        self.fresh_stores()
        lines = [f"{r.run_id}: {r.detail}" for r in poll_live_sessions(self.home, managers=self.managers)]
        lines.extend(
            Controller(
                self.home, managers=dict(self.managers), clock=self.clock, api_base="http://127.0.0.1:9"
            )
            .tick()
            .lines
        )
        lines.extend(auth_recovery.tick(self.home, managers=dict(self.managers), clock=self.clock))
        return lines

    def ticks(self, *moments: float) -> List[str]:
        lines: List[str] = []
        for moment in moments:
            lines.extend(self.tick(moment))
        return lines

    def stop(self, run_id: str, *, source: str = "gui") -> Any:
        return DispatchLedger(
            self.home,
            managers=dict(self.managers),
            session_command=[sys.executable, str(self.cli)],
        ).cancel(run_id, actor=PERSON, requester=PERSON, source=source)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    built = World(tmp_path, monkeypatch)
    yield built
    close_execution_stores()


def fire_retry(world: World, start: float) -> List[str]:
    """Schedule a retry, move past its delay (the policy's cap plus jitter), and relaunch."""
    return world.ticks(start, start + 700, start + 710, start + 720)


def every_ten_seconds(until: float, start: float = 0.0) -> List[float]:
    moments: List[float] = []
    moment = start
    while moment <= until:
        moments.append(moment)
        moment += 10.0
    return moments


# ===== a1: the task-224 timelines ======================================================


class TestTask224:
    """Login expiry, played on the recorded timelines. a1 of task-419; o4 of task-414."""

    def _parked(self, world: World) -> Tuple[str, str]:
        task_id = world.task()
        handle = world.dispatch(task_id)
        world.stall(handle.run_id, at=0)
        world.tick(0)
        return task_id, handle.run_id

    def test_a_store_that_heals_itself_needs_nobody_and_the_run_reaches_review(
        self, world: World
    ) -> None:
        timeline = load_fixture("task-224")["timelines"]["self_heal"]
        recovers = next(e["at"] for e in timeline["events"] if e["kind"] == "store_recovers")
        task_id, run_id = self._parked(world)
        assert world.get(task_id).ball is Ball.AGENT, "nobody is paged for a store that may heal"

        for moment in every_ten_seconds(until=recovers - 1, start=10):
            world.tick(moment)
        assert world.calls("nudges.log") == [], "no work turn is sent against a dead store"

        world.store_answers("ready")
        world.reply_when_woken(run_id, at=recovers + 70)
        for moment in every_ten_seconds(until=recovers + 120, start=recovers):
            world.tick(moment)

        nudges = world.calls("nudges.log")
        assert len(nudges) == timeline["expected"]["resumes"]
        assert nudges[0]["argv"] == ["--bg", "--resume", world.row(run_id)["sessionId"]]
        waiter = auth_recovery.book_for(world.home).waiters(world.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.RECOVERED

        # The resumed agent finishes its turn by asking for review: posture `auto`'s
        # justified wait, and the one place a person is meant to come in.
        world.managers["sandbox"].handoff(
            task_id, actor=AGENT, ball=Ball.HUMAN, ball_reason=BallReason.REVIEW,
            ball_prompt="Built and verified; please review.",
        )
        world.set_session(run_id, status="idle", state="done", pid=None)
        world.tick(recovers + 180)

        task = world.get(task_id)
        assert (task.ball, task.ball_reason) == (Ball.HUMAN, BallReason.REVIEW)
        assert world.person.actions == [] == [a for a in world.person.actions]
        assert pages(task) == [], "no notification at all"
        assert find_run(world.home, run_id).status == "finished"
        assert len(world.live_sessions(task_id)) == 0

    def test_a_dead_store_pages_once_for_a_login_and_needs_no_answer_or_dispatch(
        self, world: World
    ) -> None:
        timeline = load_fixture("task-224")["timelines"]["dead_store"]
        moments = {e["kind"]: e["at"] for e in timeline["events"] if e["kind"] != "historical_message"}
        task_id, run_id = self._parked(world)
        dispatches = len(dispatch_entries(world.get(task_id)))

        for moment in every_ten_seconds(until=moments["login"] - 1, start=10):
            world.tick(moment)
        task = world.get(task_id)
        assert len(pages(task)) == 1, "one notification, at the five-minute deadline"
        # The historical message at +317 s is not asked for, and the harness does not send
        # it: the prompt says so, and answering could not have worked against this store.
        assert "no Answer and no Dispatch" in (task.ball_prompt or "")

        world.person.act("login", task, because="claude auth login")
        world.clock.move_to(moments["login"])
        world.tick(moments["store_recovers"] - 1)
        world.store_answers("ready")
        world.reply_when_woken(run_id, at=moments["store_recovers"] + 70)
        for moment in every_ten_seconds(until=moments["store_recovers"] + 120, start=moments["store_recovers"]):
            world.tick(moment)

        task = world.get(task_id)
        assert (task.ball, task.ball_reason) == (Ball.AGENT, BallReason.WORK), "cleared by itself"
        assert len(world.calls("nudges.log")) == timeline["expected"]["resumes"]
        assert [kind for kind, _ in world.person.actions] == ["login"]
        assert len(pages(task)) == timeline["expected"]["notifications"]
        assert len(dispatch_entries(task)) == dispatches, "no Dispatch click, no new run"
        assert find_run(world.home, run_id).status == "running", "the same run carries on"

    def test_a_spend_limit_answer_is_never_taken_for_a_healthy_store(self, world: World) -> None:
        timeline = load_fixture("task-224")["timelines"]["spend_limit_probe"]
        refusal = next(e for e in timeline["events"] if e["kind"] == "probe_output")
        task_id, run_id = self._parked(world)
        world.store_answers("refused", text=refusal["text"], exit=refusal["exit"])
        world.reply_when_woken(run_id, at=5)

        for moment in every_ten_seconds(until=600, start=10):
            world.tick(moment)

        assert world.calls("nudges.log") == [], "billing is not success"
        probes = auth_recovery.book_for(world.home).probes(
            auth_recovery.book_for(world.home).incident(world.meta(run_id)["auth_incident"]).profile_key  # type: ignore[union-attr]
        )
        assert probes and all(
            (p.get("result") or {}).get("class") != "ready" for p in probes
        )
        waiter = auth_recovery.book_for(world.home).waiters(world.meta(run_id)["auth_incident"])[0]
        assert waiter.status != auth_recovery.RECOVERED
        task = world.get(task_id)
        assert task.ball is Ball.HUMAN, "still refused at the deadline: a person is asked"


# ===== a1, a3, a4: the task-410 timeline, forked at its unexplained cancellation ========


def _answer(world: World, task_id: str, body: str, project_id: str = "sandbox") -> int:
    """A person's answer to a parked run: unsolicited here, so a message and not an action."""
    world.person.message(body)
    task = world.managers[project_id].handoff(
        task_id,
        actor=PERSON,
        ball=Ball.AGENT,
        ball_reason=BallReason.ANSWER,
        ball_prompt=body,
    )
    return int(task.log[-1].id)


def _worker_gone(world: World, run_id: str) -> None:
    """The session vanishes from the listing, as a machine restart or a crash leaves it."""
    world.set_session(run_id, state="stopped", status="stopped", pid=None)


class TestTask410:
    """task-410: a big-dawg autonomous dispatch, a login expiry one second in, an answer
    buffered for the parked run, and a cancellation nobody recorded asking for."""

    def _dispatched_and_parked(self, world: World) -> Tuple[str, Any, Dict[str, Any]]:
        fixture = load_fixture("task-410")
        events = {event["kind"]: event for event in fixture["events"]}
        task_id = world.task(title="Big Dawg Audit")
        handle = world.dispatch(
            task_id, group=fixture["dispatch"]["group"], posture=Posture.AUTONOMOUS
        )
        world.stall(handle.run_id, at=events["stall"]["at"])
        world.tick(events["stall"]["at"])
        # The defaults move underneath the run, as they had by the time task-410 resumed:
        # a person dispatching afresh today would get the default group's runner.
        world.configure(group="default", posture="auto")
        return task_id, handle, events

    def test_without_a_stop_the_answer_rides_the_one_resume_and_nothing_is_downgraded(
        self, world: World
    ) -> None:
        task_id, handle, events = self._dispatched_and_parked(world)
        task = world.get(task_id)
        assert pages(task) == [], "the historical page at +5 s is not repeated"
        [launch] = world.calls("launches.log")
        assert launch["model"] == "claude-fable-5-1"

        answer_at = events["answer"]["at"]
        world.clock.move_to(answer_at)
        body = events["answer"]["body"]
        _answer(world, task_id, body)
        world.store_answers("ready")
        world.reply_when_woken(handle.run_id, at=answer_at + 40)
        world.ticks(answer_at, answer_at + 30, answer_at + 60)

        nudges = world.calls("nudges.log")
        assert len(nudges) == 1, "exactly one safe continuation"
        assert body in nudges[0]["stdin"], "the buffered answer is delivered, not dropped"
        assert "releases the merge gate" in nudges[0]["stdin"], "the granted policy clause"
        assert nudges[0]["argv"] == ["--bg", "--resume", world.row(handle.run_id)["sessionId"]]
        assert len(world.calls("launches.log")) == 1, "resumed in place, not relaunched"

        # Later the session is lost outright. The controller retries it, on the envelope
        # the owner granted -- not on today's defaults.
        _worker_gone(world, handle.run_id)
        world.tick(answer_at + 120)
        fire_retry(world, answer_at + 130)
        task = world.get(task_id)
        dispatches = dispatch_entries(task)
        assert len(dispatches) == 2, [d.get("run_id") for d in dispatches]
        retry = dispatches[-1]
        assert retry["runner"] == "fable" and retry["selection"]["group"] == "big-dawg"
        assert retry["selection"]["source"] == "history"
        assert retry["posture"] == "autonomous"
        assert world.calls("launches.log")[-1]["model"] == "claude-fable-5-1"
        assert len(world.live_sessions(task_id)) == 1, "one writer"

        # The retried agent finishes: an autonomous run closes its own task.
        world.managers["sandbox"].close_task(task_id, actor=AGENT, outcome="completed")
        world.set_session(retry["run_id"], status="idle", state="done", pid=None)
        world.tick(answer_at + 1000)

        task = world.get(task_id)
        assert task.lifecycle is Lifecycle.CLOSED
        assert world.person.actions == [], "zero human actions after the authorisation"
        assert pages(task) == []
        assert all("nobody was told" not in (entry.body or "") for entry in task.log)

    def test_an_explicit_stop_ends_it_and_nothing_continues_even_after_a_restart(
        self, world: World
    ) -> None:
        task_id, handle, events = self._dispatched_and_parked(world)
        world.clock.move_to(events["answer"]["at"])
        answer = _answer(world, task_id, events["answer"]["body"])
        world.tick(events["answer"]["at"])

        stop_at = events["cancellation"]["at"]
        world.clock.move_to(stop_at)
        stopped = world.stop(handle.run_id)
        assert stopped.stopped, stopped.detail

        # The store answers after the Stop, and the machine restarts more than once.
        world.store_answers("ready")
        world.reply_when_woken(handle.run_id, at=stop_at + 60)
        world.ticks(stop_at + 10, stop_at + 70, stop_at + 130, stop_at + 900, stop_at + 1800)

        assert world.calls("nudges.log") == [], "a Stop suppresses the recovery nudge"
        assert len(world.calls("launches.log")) == 1, "and every relaunch"
        assert world.live_sessions(task_id) == []
        store = journal(world.home)
        attempt = store.attempt(handle.run_id)
        assert attempt is not None and attempt.cancel_requested
        assert (attempt.cancel or {}).get("requester") == PERSON
        assert (attempt.cancel or {}).get("source") == "gui"
        execution = store.latest_execution("sandbox", task_id)
        assert execution is not None and execution.terminal and execution.state == "cancelled"
        waiters = auth_recovery.book_for(world.home).waiters(world.meta(handle.run_id)["auth_incident"])
        assert [w.status for w in waiters] == [auth_recovery.REMOVED]

        task = world.get(task_id)
        assert "nobody was told" not in (task.ball_prompt or "")
        assert PERSON in (task.ball_prompt or ""), "the Stop says whose it was"
        assert len(dispatch_entries(task)) == 1
        assert answer in [entry.id for entry in task.log], "the answer itself is kept"

    def test_an_administrative_stand_down_keeps_the_answer_and_the_approval_and_merges_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other fork. Nothing revokes the intent: a finish takes the task over from the
        parked session, and what the person said and approved survives the transfer."""
        import test_dispatch_finish
        from agentjobs.api.routes.tasks import APPROVAL_CLEARANCE
        from agentjobs.dispatch.approval import accept_signals, approval_data, reviewed_branches
        from agentjobs.dispatch.finish import FINISHED, finish_task

        built = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
            tmp_path, monkeypatch
        )
        world = World(tmp_path, monkeypatch, projects=())
        world.adopt("demo", built["root"], built["manager"])
        manager, task_id = built["manager"], built["task_id"]
        fixture = load_fixture("task-410")
        events = {event["kind"]: event for event in fixture["events"]}

        handle = world.dispatch(task_id, project_id="demo")
        manager.handoff(
            task_id, actor=AGENT, ball=Ball.HUMAN, ball_reason=BallReason.REVIEW,
            ball_prompt="Built and verified; please review.",
        )
        world.stall(handle.run_id, at=events["stall"]["at"])
        world.tick(events["stall"]["at"])
        assert auth_recovery.book_for(world.home).active_waiter_for_run(handle.run_id)

        world.clock.move_to(events["answer"]["at"])
        answer = _answer(world, task_id, events["answer"]["body"], project_id="demo")
        world.tick(events["answer"]["at"])

        # The person approves, with a note, and the approval route's own receipt.
        manager.handoff(
            task_id, actor=AGENT, ball=Ball.HUMAN, ball_reason=BallReason.REVIEW,
            ball_prompt="Answered; ready for review again.",
        )
        task = world.get(task_id, "demo")
        world.person.act("approve", task, because="review")
        note = "Also say which driver this was proved on."
        approved = manager.handoff(
            task_id, actor=PERSON, ball=Ball.AGENT, ball_reason=BallReason.WORK,
            ball_prompt=APPROVAL_CLEARANCE,
            body=f"Approved by {PERSON} through the web UI.",
            data=approval_data(
                approver=PERSON, note=note, reviewed=reviewed_branches(built["root"], task)
            ),
        )
        accept_signals(world.home, "demo", approved, [approved.log[-1]])
        approval = int(approved.log[-1].id)

        world.clock.move_to(events["cancellation"]["at"])
        result = finish_task(
            manager=manager, project=built["project"], task_id=task_id, approver=PERSON,
            home=world.home, api_base="http://127.0.0.1:1",
            settings=test_dispatch_finish.settings(),
        )
        assert result.outcome == FINISHED, result.render()

        # Recovery never talks over the transfer, however the store answers afterwards.
        world.store_answers("ready")
        world.ticks(events["cancellation"]["at"] + 60, events["cancellation"]["at"] + 120)
        assert world.calls("nudges.log") == []

        store = journal(world.home)
        attempt = store.attempt(handle.run_id)
        assert attempt is not None and not attempt.is_live
        assert attempt.cancel_requested is False, "a transfer is never a cancellation"
        assert attempt.outcome == "completed"
        kinds = [e.kind for e in store.events(attempt.execution_id or "")]
        assert "stand_down_requested" in kinds and "stop_requested" not in kinds
        merges = subprocess.run(
            ["git", "-C", str(built["root"]), "rev-list", "--merges", "main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        assert len(merges) == 1, "one merge"

        task = world.get(task_id, "demo")
        assert task.lifecycle is Lifecycle.CLOSED
        entries = {entry.id: entry for entry in task.log}
        assert entries[answer].body and events["answer"]["body"] in (entries[answer].body or "")
        assert entries[approval].data["approval"]["note"] == note, "the note is kept verbatim"
        from agentjobs.dispatch.approval import source_event
        from agentjobs.execution.coordinator import task_feed_source

        for kept in (answer, approval):
            row = store.signal(
                task_feed_source("demo"), source_event("demo", task_id, entries[kept]).source_event_id
            )
            assert row is not None
            disposition = store.signal_disposition(task_feed_source("demo"), row.source_event_id)
            assert not str((disposition or {}).get("by") or "").startswith("stop:"), disposition
        assert all("nobody was told" not in (entry.body or "") for entry in task.log)
        assert [kind for kind, _ in world.person.actions] == ["approve"], (
            "the deliberate review an `auto` posture requires, and nothing else"
        )
