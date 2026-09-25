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
``FakeClock`` over ``skipping_clock.SkippingClock``, installed over ``agentjobs.clock`` for
the fixture's life, so every production call site reads it rather than only the objects the
harness hands one to (task-518); nothing here sleeps; a subprocess is either a fake CLI
invocation that answers at once or a child that dies at a named line, and the child installs
the same clock from its argv. Two clocks cannot disagree under load because there is one.

**Human actions are counted, and so is delivery.** ``Person`` performs an action only when
the task record asks for it, and records it. A scenario asserts the count *and* that the run
reached its terminal state or the one wait the design justifies -- a harness that did
nothing would otherwise score a perfect zero.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

from agentjobs.dispatch import auth_recovery, program
from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.auth import CLAUDE_HOME_ENV
from agentjobs.dispatch.controller import Controller
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import DispatchLedger, find_run
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import (
    SESSION_NAME_PATTERN,
    RunDirectory,
    runs_root,
)
from agentjobs.execution.factory import close_execution_stores
from agentjobs.manager import TaskManager
from agentjobs.dispatch.config import MergeMode
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome
from agentjobs.projects import ProjectRegistry
import fake_claude
from skipping_clock import SkippingClock
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

# The fake lives in `fake_claude.py` beside this file, so the crash children -- separate
# interpreters on purpose -- run it as a script and this process answers it in-process.
FAKE_CLAUDE = HERE / "fake_claude.py"


class FakeClock:
    """This harness's face on the subsystem's one clock (task-518).

    The name and the four members are what the scenarios below were written against;
    underneath is :class:`skipping_clock.SkippingClock`, installed over
    :mod:`agentjobs.clock` for the fixture's whole life. The difference is which
    call sites it reaches. This used to be a clock that had to be *handed* to every
    object, so a production call site reading the wall clock directly -- and there were
    forty-three of those -- was simply not on this timeline. That is a second clock by
    another name, and two clocks that can disagree is the bug the epic exists to remove.

    It starts where the machine is, rather than two hours ahead as task-414's did; the
    reason that skew existed is gone and the reason it now has to be zero is in
    ``skipping_clock.DEFAULT_SKEW``. Offsets are seconds since ``zero``, which a scenario
    sets at its fixture's ``at: 0``.
    """

    def __init__(self) -> None:
        self.skipping = SkippingClock()
        self._reached = 0.0

    @property
    def zero(self) -> datetime:
        return self.skipping.origin

    @property
    def offset(self) -> float:
        """The moment a scenario has moved to, not the clock's own microsecond drift."""
        return self._reached

    def __call__(self) -> datetime:
        return self.skipping.now()

    def at(self, seconds: float) -> datetime:
        return self.skipping.at(seconds)

    def move_to(self, seconds: float) -> None:
        """Let the poller's own wait carry the clock forward, rather than setting it.

        A scenario still names absolute fixture moments; what changed is that reaching one
        is a wait the production code's own sleep would have made, so the clock moves for
        the reason it moves in production rather than because a test assigned to it.
        """
        remaining = seconds - self._reached
        assert remaining >= 0, "a fixture clock never runs backwards"
        self._reached = seconds
        if remaining > 0:
            dispatch_clock.sleep(remaining)


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# ----- who touched what ---------------------------------------------------------------


def task_of(name: Any) -> str:
    """The task id inside a dispatched session's name, read with the production grammar.

    The sibling of ``test_execution_controller.task_of``; this harness is standalone and
    imports nothing from it.
    """
    match = SESSION_NAME_PATTERN.match(str(name or ""))
    assert match is not None, f"not a dispatched session name: {name!r}"
    return match.group("task")


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
        assert because in (
            task.ball_prompt or ""
        ), f"{kind} was not what the record asked for: {task.ball_prompt!r}"
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
        clock: Optional["FakeClock"] = None,
    ) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.claude_home = tmp_path / "claude"
        self.claude_home.mkdir()
        self.cli_dir = tmp_path / "cli"
        self.cli_dir.mkdir()
        self.cli = self.cli_dir / "claude.py"
        shutil.copyfile(FAKE_CLAUDE, self.cli)
        # **What is real and what is answered in-process** (task-525). This process asks
        # the fake CLI for its listing, transcript, probes and launches in-process: the
        # scenarios are about what dispatch decides from those answers, and task-518
        # counted TestTask224 alone spawning 120 interpreters to get them. A crash child
        # installs nothing, so every scenario that is about a process -- a death at a
        # named line, a fresh coordinator after it -- still starts real ones, and they
        # run the same `fake_claude.answer` as a script.
        cli_dir = self.cli_dir
        monkeypatch.setitem(
            program.INSTALLED,
            program.key(str(self.cli)),
            lambda argv, stdin, cwd: fake_claude.answer(argv, lambda: stdin, cwd, cli_dir),
        )
        self.store_answers("refused")
        self.clock = clock if clock is not None else FakeClock()
        # Installed here rather than in the fixture, because two scenarios build a World
        # directly and a World whose clock nothing reads is the two-timeline bug again.
        monkeypatch.setattr(dispatch_clock, "INSTALLED", self.clock.skipping)
        self.person = Person()
        self.managers: Dict[str, Any] = {}
        self.roots: Dict[str, Path] = {}
        monkeypatch.setenv("AGENTJOBS_HOME", str(self.home))
        monkeypatch.setenv(CLAUDE_HOME_ENV, str(self.claude_home))
        monkeypatch.delenv("AGENTJOBS_RUN_ID", raising=False)
        # Nothing is monkeypatched to make a runner read this clock. Every runner the code
        # under test builds -- the dispatcher's, the poller's, the controller's, auth
        # recovery's -- reads `agentjobs.clock`, and so does every call site that used to
        # read the wall clock behind their backs (task-518).
        #
        # The harness drives the controller and the recovery pass itself, so a poll here
        # only follows sessions rather than running them twice.
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
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
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
        subprocess.run(
            ["git", "add", "--", ".gitignore"], cwd=root, capture_output=True, check=True
        )
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
        merge_mode: str = "review",
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
                    "merge_mode": merge_mode,
                    "allow_automerge": True,
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
        """The fake CLI's listed session for one run.

        Matched on the session id the launcher printed and the run recorded, which is the
        correlation the run record itself holds. It used to be matched on the run stub in
        the session name, which task-452 took out: the name now says which *task* a
        session is working, not which run, and telling two runs of one task apart from
        the name alone was never something a name could be relied on for.
        """
        meta = RunDirectory(path=runs_root(self.home) / run_id).read_meta()
        [found] = [r for r in self.rows() if r["id"] == meta.get("session_id")]
        return found

    def live_sessions(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Sessions the fake CLI still lists, optionally only one task's.

        Read with the production grammar rather than by hand: the name has carried a run
        stub, an ordinal, a conditional project prefix and now a slug, and a test that
        re-derives the task id from it goes red every time one of those changes.
        """
        rows = [r for r in self.rows() if r.get("state") != "stopped"]
        if task_id is None:
            return rows
        return [r for r in rows if task_of(r["name"]) == task_id]

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
        return str(created.id)

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
        merge_mode: Optional[MergeMode] = None,
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
                merge_mode=merge_mode,
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
        lines = [
            f"{r.run_id}: {r.detail}" for r in poll_live_sessions(self.home, managers=self.managers)
        ]
        lines.extend(
            Controller(
                self.home,
                managers=dict(self.managers),
                api_base="http://127.0.0.1:9",
            )
            .tick()
            .lines
        )
        lines.extend(auth_recovery.tick(self.home, managers=dict(self.managers)))
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
    """A machine whose clock the dispatch subsystem reads, for the test's whole length."""
    built = World(tmp_path, monkeypatch)
    yield built
    close_execution_stores()


def fire_retry(world: World, start: float) -> List[str]:
    """Schedule a retry, move past its delay (the policy's cap plus jitter), and relaunch."""
    return world.ticks(start, start + 700, start + 710, start + 720)


def every_thirty_seconds(until: float, start: float = 0.0) -> List[float]:
    moments: List[float] = []
    moment = start
    while moment <= until:
        moments.append(moment)
        moment += 30.0
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

        for moment in every_thirty_seconds(until=recovers - 1, start=10):
            world.tick(moment)
        assert world.calls("nudges.log") == [], "no work turn is sent against a dead store"

        world.store_answers("ready")
        world.reply_when_woken(run_id, at=recovers + 70)
        for moment in every_thirty_seconds(until=recovers + 120, start=recovers):
            world.tick(moment)

        nudges = world.calls("nudges.log")
        assert len(nudges) == timeline["expected"]["resumes"]
        assert nudges[0]["argv"] == ["--bg", "--resume", world.row(run_id)["sessionId"]]
        waiter = auth_recovery.book_for(world.home).waiters(world.meta(run_id)["auth_incident"])[0]
        assert waiter.status == auth_recovery.RECOVERED

        # The resumed agent finishes its turn by asking for review: posture `auto`'s
        # justified wait, and the one place a person is meant to come in.
        world.managers["sandbox"].handoff(
            task_id,
            actor=AGENT,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
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
        moments = {
            e["kind"]: e["at"] for e in timeline["events"] if e["kind"] != "historical_message"
        }
        task_id, run_id = self._parked(world)
        dispatches = len(dispatch_entries(world.get(task_id)))

        for moment in every_thirty_seconds(until=moments["login"] - 1, start=10):
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
        for moment in every_thirty_seconds(
            until=moments["store_recovers"] + 120, start=moments["store_recovers"]
        ):
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

        for moment in every_thirty_seconds(until=600, start=10):
            world.tick(moment)

        assert world.calls("nudges.log") == [], "billing is not success"
        probes = auth_recovery.book_for(world.home).probes(
            auth_recovery.book_for(world.home).incident(world.meta(run_id)["auth_incident"]).profile_key  # type: ignore[union-attr]
        )
        assert probes and all((p.get("result") or {}).get("class") != "ready" for p in probes)
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
            task_id, group=fixture["dispatch"]["group"], merge_mode=MergeMode.AUTOMERGE
        )
        world.stall(handle.run_id, at=events["stall"]["at"])
        world.tick(events["stall"]["at"])
        # The defaults move underneath the run, as they had by the time task-410 resumed:
        # a person dispatching afresh today would get the default group's runner.
        world.configure(group="default", merge_mode="review")
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
        assert retry["merge_mode"] == "automerge"
        assert world.calls("launches.log")[-1]["model"] == "claude-fable-5-1"
        assert len(world.live_sessions(task_id)) == 1, "one writer"

        # The retried agent finishes: an autonomous run closes its own task.
        world.managers["sandbox"].close_task(task_id, actor=AGENT, outcome=Outcome.COMPLETED)
        world.set_session(retry["run_id"], status="idle", state="done", pid=None)
        world.tick(answer_at + 1000)

        task = world.get(task_id)
        assert task.lifecycle is Lifecycle.CLOSED
        retried = journal(world.home).attempt(retry["run_id"])
        assert retried is not None and not retried.is_live, (
            "the retried attempt is followed to its end: a signal pending across the retry "
            "once wedged every replay of this execution (task-419)"
        )
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
        waiters = auth_recovery.book_for(world.home).waiters(
            world.meta(handle.run_id)["auth_incident"]
        )
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

        # The finish fixture hands over a task already claimed by `claude`, which is how
        # a finish needs to find one; this test dispatches at it first, so the claim is
        # given back and the dispatch makes its own, exactly as a real one does. Without
        # that the dispatch arrives at a claim nobody started -- task-179's refusal, and
        # correctly so: a claim with no handover and nothing in the ledger is the
        # 2026-08-19 defect whatever wrote it.
        manager.release_task(task_id, actor=PERSON, body="Dispatching at it instead.")
        handle = world.dispatch(task_id, project_id="demo")
        manager.handoff(
            task_id,
            actor=AGENT,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
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
            task_id,
            actor=AGENT,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Answered; ready for review again.",
        )
        task = world.get(task_id, "demo")
        world.person.act("approve", task, because="review")
        note = "Also say which driver this was proved on."
        approved = manager.handoff(
            task_id,
            actor=PERSON,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
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
            manager=manager,
            project=built["project"],
            task_id=task_id,
            approver=PERSON,
            home=world.home,
            api_base="http://127.0.0.1:1",
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
            capture_output=True,
            text=True,
            check=True,
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
                task_feed_source("demo"),
                source_event("demo", task_id, entries[kept]).source_event_id,
            )
            assert row is not None
            disposition = store.signal_disposition(task_feed_source("demo"), row.source_event_id)
            assert not str((disposition or {}).get("by") or "").startswith("stop:"), disposition
        assert all("nobody was told" not in (entry.body or "") for entry in task.log)
        assert [kind for kind, _ in world.person.actions] == [
            "approve"
        ], "the deliberate review an `auto` posture requires, and nothing else"


# ===== a2: every documented crash boundary, with a real process death at each =========

CHILD = r"""
import os, pathlib, sys
from datetime import datetime, timedelta
home, tests, action, point = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
zero, offset, subject = datetime.fromisoformat(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
sys.path.insert(0, tests)
from support import task_store
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
import agentjobs.dispatch.runner as runner_module

class Frozen:
    # A separate process, so it inherits nothing of the parent's clock but the two numbers
    # on its argv. It installs one rather than handing it to a runner, which is the whole
    # of task-518: the crash boundary this child exists to cross runs through call sites
    # nobody passes a clock to.
    def now(self):
        return zero + timedelta(seconds=offset)
    def monotonic(self):
        return offset
    def sleep(self, seconds):
        pass

import agentjobs.clock as clock_module
clock_module.INSTALLED = Frozen()
clock = clock_module.utcnow
registry = ProjectRegistry(home=home)
managers = {p.id: TaskManager(task_store(p.root / "tasks", project_id=p.id)) for p in registry.list_projects()}

def die(*args, **kwargs):
    sys.stdout.flush()
    os._exit(9)

if point == "before_admission":
    from agentjobs.execution.store import ExecutionStore
    ExecutionStore.admit = die
elif point == "before_marker":
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
elif point == "nudge_before_effect":
    from agentjobs.dispatch.auth_recovery import ClaudeSessionNudger
    ClaudeSessionNudger.nudge = die
elif point == "nudge_after_effect":
    from agentjobs.dispatch.auth_recovery import ClaudeSessionNudger
    real_nudge = ClaudeSessionNudger.nudge
    def nudge_then_die(self, session_id, message):
        real_nudge(self, session_id, message)
        die()
    ClaudeSessionNudger.nudge = nudge_then_die
elif point == "outbox_before_write":
    import agentjobs.dispatch.journal as journal_module
    journal_module.apply_projection = die
elif point == "outbox_before_ack":
    from agentjobs.execution.store import ExecutionStore
    ExecutionStore.acknowledge = die
elif point == "merge_before_receipt":
    from agentjobs.dispatch.finish_receipts import FinishReceipts, APPLIED
    real_settle = FinishReceipts.settle
    def settle(self, finish_id, activity, key, state, **fields):
        if activity == "merge" and state == APPLIED:
            die()
        return real_settle(self, finish_id, activity, key, state, **fields)
    FinishReceipts.settle = settle
elif point == "none":
    pass
else:
    raise SystemExit("unknown point " + point)

if action == "dispatch":
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
    task_id, caused_by = subject.split(":")
    project = registry.get("sandbox")
    dispatch_task(
        manager=managers["sandbox"], project=project, project_config=project.load_config(),
        request=DispatchRequest(task_id=task_id, caused_by=int(caused_by)),
        home=home, api_base="http://127.0.0.1:9", now=clock(),
    )
elif action == "controller":
    from agentjobs.dispatch.controller import Controller
    Controller(home, managers=managers, clock=clock, api_base="http://127.0.0.1:9").tick()
elif action == "auth":
    from agentjobs.dispatch import auth_recovery
    auth_recovery.tick(home, managers=managers, clock=clock)
elif action == "finish":
    import test_dispatch_finish
    from agentjobs.dispatch import finish
    finish.worktree_interpreter = lambda path: pathlib.Path(sys.executable)
    project = registry.get("demo")
    finish.finish_task(
        manager=managers["demo"], project=project, task_id=subject, approver="Jeff Posey",
        home=home, api_base="http://127.0.0.1:1", settings=test_dispatch_finish.settings(),
    )
print("survived")
"""


def die_in_child(world: World, action: str, point: str, subject: str = "-") -> None:
    """Run one production action in a fresh interpreter that dies at ``point``."""
    run_in_child(world, action, point, subject, expect=9)


def run_in_child(
    world: World, action: str, point: str, subject: str = "-", *, expect: int = 0
) -> None:
    """Run one production action in a fresh interpreter: another process on the machine."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SOURCE), str(TESTS), env.get("PYTHONPATH", "")])
    env["AGENTJOBS_HOME"] = str(world.home)
    env[CLAUDE_HOME_ENV] = str(world.claude_home)
    env.pop("AGENTJOBS_RUN_ID", None)
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(world.home),
            str(TESTS),
            action,
            point,
            world.clock.zero.isoformat(),
            str(world.clock.offset),
            subject,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert (
        done.returncode == expect
    ), f"{action}/{point} exited {done.returncode}, not {expect}:\n{done.stdout}\n{done.stderr}"


def assert_no_duplicate_effects(world: World, task_id: str, project_id: str = "sandbox") -> None:
    """The a2 invariants, asserted after every boundary: one writer, one write per operation."""
    assert len(world.live_sessions(task_id)) <= 1, "a second live writer"
    task = world.get(task_id, project_id)
    operations = [
        (entry.data.get("operation") or {}).get("id")
        for entry in task.log
        if (entry.data.get("operation") or {}).get("id")
    ]
    assert len(operations) == len(set(operations)), "an accepted log mutation was written twice"
    results = [e.data.get("run_id") for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]
    assert len(results) == len(set(results)), "a run's ending was recorded twice"
    launched = [e.data.get("run_id") for e in task.log if e.type is LogEntryType.DISPATCH]
    assert len(launched) == len(set(launched)), "a run was recorded as dispatched twice"


class TestLaunchBoundaries:
    """Intent, effect and result around a launch: a real death at each, then fresh ticks."""

    def _crash(self, world: World, point: str) -> str:
        task_id = world.task()
        caused_by = world.authorise(task_id)
        die_in_child(world, "dispatch", point, f"{task_id}:{caused_by}")
        return task_id

    def test_before_the_intent_commits_nothing_is_owned_and_nothing_launched(
        self, world: World
    ) -> None:
        task_id = self._crash(world, "before_admission")
        assert journal(world.home).latest_execution("sandbox", task_id) is None
        world.ticks(10, 800)
        assert world.calls("launches.log") == []
        assert_no_duplicate_effects(world, task_id)
        # Nothing was accepted, so nothing is owed -- and nothing blocks a person's retry.
        world.dispatch(task_id)
        assert len(world.live_sessions(task_id)) == 1

    def test_after_the_intent_and_before_the_effect_it_is_retried_once(self, world: World) -> None:
        task_id = self._crash(world, "before_marker")
        world.tick(10)
        fire_retry(world, 20)
        assert len(world.calls("launches.log")) == 1
        assert len(world.live_sessions(task_id)) == 1
        fire_retry(world, 800)
        assert len(world.calls("launches.log")) == 1, "replaying again launches nothing"
        assert_no_duplicate_effects(world, task_id)

    def test_an_effect_whose_result_was_lost_is_found_stopped_and_replaced_once(
        self, world: World
    ) -> None:
        """The launcher printed an id nobody recorded: success followed by a lost result."""
        task_id = self._crash(world, "after_launch")
        assert len(world.live_sessions(task_id)) == 1
        world.tick(10)
        assert world.live_sessions(task_id) == [], "unfollowable, so stopped and confirmed"
        fire_retry(world, 20)
        fire_retry(world, 800)
        assert len(world.calls("launches.log")) == 2
        assert len(world.live_sessions(task_id)) == 1, "one replacement, never two"
        assert_no_duplicate_effects(world, task_id)

    def test_a_death_before_the_result_commits_reattaches_without_a_second_launch(
        self, world: World
    ) -> None:
        task_id = self._crash(world, "before_launched")
        world.ticks(10, 20, 800, 1600)
        assert len(world.calls("launches.log")) == 1
        attempt = journal(world.home).attempts_for(
            journal(world.home).latest_execution("sandbox", task_id).execution_id  # type: ignore[union-attr]
        )
        assert [a.state for a in attempt] == ["live"]
        assert_no_duplicate_effects(world, task_id)


class TestNudgeBoundaries:
    """A resume message is an effect with no idempotency key: it is never sent twice."""

    def _parked_and_ready(self, world: World) -> Tuple[str, str]:
        task_id = world.task()
        handle = world.dispatch(task_id)
        world.stall(handle.run_id, at=0)
        world.tick(0)
        world.store_answers("ready")
        return task_id, handle.run_id

    def test_a_death_between_intent_and_effect_is_escalated_once_and_never_resent(
        self, world: World
    ) -> None:
        task_id, run_id = self._parked_and_ready(world)
        world.clock.move_to(60)
        die_in_child(world, "auth", "nudge_before_effect")
        waiter = auth_recovery.book_for(world.home).active_waiter_for_run(run_id)
        assert waiter is not None and waiter.status == auth_recovery.NUDGING
        world.ticks(120, 400, 700, 1300)
        assert world.calls("nudges.log") == [], "unprovable, so not sent again"
        task = world.get(task_id)
        assert len(pages(task)) == 1 and task.ball is Ball.HUMAN
        assert_no_duplicate_effects(world, task_id)

    def test_a_death_after_the_effect_is_recognised_from_the_reply_and_not_resent(
        self, world: World
    ) -> None:
        task_id, run_id = self._parked_and_ready(world)
        world.reply_when_woken(run_id, at=62)
        world.clock.move_to(60)
        die_in_child(world, "auth", "nudge_after_effect")
        assert len(world.calls("nudges.log")) == 1
        world.ticks(120, 400, 700, 1300)
        assert len(world.calls("nudges.log")) == 1, "delivered once"
        waiter = auth_recovery.book_for(world.home).waiters(world.meta(run_id)["auth_incident"])[0]
        assert waiter.status in (auth_recovery.NUDGED, auth_recovery.RECOVERED)
        assert pages(world.get(task_id)) == []
        assert_no_duplicate_effects(world, task_id)


class TestOutboxBoundaries:
    """A run's ending is owed to its task through the outbox: before and after the write."""

    def _ended(self, world: World) -> Tuple[str, str]:
        task_id = world.task()
        handle = world.dispatch(task_id)
        world.managers["sandbox"].handoff(
            task_id,
            actor=AGENT,
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Built; please review.",
        )
        world.set_session(handle.run_id, status="idle", state="done", pid=None)
        world.clock.move_to(30)
        return task_id, handle.run_id

    @pytest.mark.parametrize("point", ["outbox_before_write", "outbox_before_ack"])
    def test_an_owed_result_is_written_exactly_once_after_the_death(
        self, world: World, point: str
    ) -> None:
        task_id, run_id = self._ended(world)
        die_in_child(world, "controller", point)
        attempt = journal(world.home).attempt(run_id)
        assert attempt is not None and not attempt.is_live, "the conclusion itself committed"
        # Startup recovery, then ordinary ticks, each a fresh process's worth of objects.
        world.fresh_stores()
        DispatchLedger(world.home, managers=dict(world.managers)).reconcile()
        world.ticks(60, 120)
        DispatchLedger(world.home, managers=dict(world.managers)).reconcile()
        task = world.get(task_id)
        results = [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]
        assert [e.data.get("run_id") for e in results] == [run_id]
        assert journal(world.home).pending_outbox() == []
        assert_no_duplicate_effects(world, task_id)


def test_a_handoff_the_api_never_imported_reaches_the_inbox_once(world: World) -> None:
    """The API committed the handoff and died before telling the journal: the feed finds it."""
    from agentjobs.dispatch.approval import accept_signals, source_event
    from agentjobs.execution.coordinator import task_feed_source

    task_id = world.task()
    handle = world.dispatch(task_id)
    entry_id = _answer(world, task_id, "One more thing.")
    world.ticks(10, 20)
    task = world.get(task_id)
    [entry] = [e for e in task.log if e.id == entry_id]
    accept_signals(world.home, "sandbox", task, [entry])  # a late synchronous accept, too
    world.tick(30)
    store = journal(world.home)
    event = source_event("sandbox", task_id, entry)
    rows = [
        row
        for row in store.inbox(project_id="sandbox")
        if row.source_event_id == event.source_event_id
    ]
    assert len(rows) == 1
    assert store.signal(task_feed_source("sandbox"), event.source_event_id) is not None
    assert len(world.live_sessions(task_id)) == 1 and handle.run_id
    assert_no_duplicate_effects(world, task_id)


def test_a_death_after_git_merge_is_resumed_by_the_poll_tick_and_merges_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """task-322's window with the finisher's process really dying after ``git merge``, and
    task-443's respawn finishing it: no person re-runs anything (o2)."""
    import test_dispatch_finish
    from agentjobs.dispatch.finish import FINISHED
    from agentjobs.dispatch.finish_resume import RESUMED, resume_interrupted_finishes
    from agentjobs.dispatch.finish_status import read_finish_status
    from test_approval_standdown import approve
    from test_finish_resume import InProcessSpawn

    built = test_dispatch_finish.world.__pytest_wrapped__.obj(  # type: ignore[attr-defined]
        tmp_path, monkeypatch
    )
    world = World(tmp_path, monkeypatch, projects=())
    world.adopt("demo", built["root"], built["manager"])
    monkeypatch.setattr(
        "agentjobs.dispatch.finish_resume.finish_is_offered", lambda *args, **kwargs: True
    )

    def merges() -> List[str]:
        return subprocess.run(
            ["git", "-C", str(built["root"]), "rev-list", "--merges", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

    manager, task_id = built["manager"], built["task_id"]
    manager.handoff(
        task_id,
        actor=AGENT,
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Built and verified; please review.",
    )
    world.person.act("approve", world.get(task_id, "demo"), because="review")
    approve(built)

    die_in_child(world, "finish", "merge_before_receipt", task_id)
    assert len(merges()) == 1, "git committed the merge before the process died"
    task = world.get(task_id, "demo")
    assert all(entry.data.get("finish_step") != "merge" for entry in task.log)

    # The server's poll tick, twice, each from fresh objects.
    spawn = InProcessSpawn(built)
    decisions = resume_interrupted_finishes(
        world.home,
        registry=ProjectRegistry(world.home),
        managers={"demo": manager},
        spawn=spawn,
    )
    assert [item.action for item in decisions] == [RESUMED]
    [result] = spawn.results
    assert result is not None and result.outcome == FINISHED, result and result.render()
    assert merges() == [result.merge_commit], "one merge, recovered rather than repeated"
    world.fresh_stores()
    assert (
        resume_interrupted_finishes(
            world.home,
            registry=ProjectRegistry(world.home),
            managers={"demo": manager},
            spawn=spawn,
        )
        == []
    )

    task = world.get(task_id, "demo")
    assert task.lifecycle is Lifecycle.CLOSED
    status = read_finish_status(world.home, task_id, project_id="demo")
    assert status is not None and status.merge_commit == result.merge_commit
    assert [kind for kind, _ in world.person.actions] == ["approve"], "no re-run by a person"
    assert pages(task) == []
    assert_no_duplicate_effects(world, task_id, "demo")


def test_the_invariants_would_catch_a_duplicate_write(world: World) -> None:
    """What ``assert_no_duplicate_effects`` catches, shown rather than assumed."""
    from agentjobs.models_v2 import DispatchOutcome

    task_id = world.task()
    handle = world.dispatch(task_id)
    manager = world.managers["sandbox"]
    manager.record_dispatch_result(
        task_id,
        actor="dispatcher",
        run_id=handle.run_id,
        outcome=DispatchOutcome.COMPLETED,
        operation_id="first-writer",
    )
    assert_no_duplicate_effects(world, task_id)
    with pytest.raises(Exception):
        # The manager itself refuses a second ending; the invariant is the backstop for a
        # path that ever bypassed it.
        manager.record_dispatch_result(
            task_id,
            actor="dispatcher",
            run_id=handle.run_id,
            outcome=DispatchOutcome.COMPLETED,
            operation_id="second-writer",
        )
    world.set_rows(world.rows() + [dict(world.row(handle.run_id), id="deadbeef")])
    with pytest.raises(AssertionError, match="second live writer"):
        assert_no_duplicate_effects(world, task_id)


# ===== o5: a five-hour usage limit waits on the service and resumes by itself ==========


def test_a_usage_limit_parks_on_the_service_and_resumes_once_after_the_reset(
    world: World,
) -> None:
    task_id = world.task()
    handle = world.dispatch(task_id, merge_mode=MergeMode.AUTOMERGE, group="big-dawg")
    resets = world.clock.at(2 * 3600)
    line = {
        "type": "assistant",
        "timestamp": stamp(world.clock.at(1)),
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{"type": "text", "text": "You've hit your session limit · resets 3:30am"}],
        },
        "error": "rate_limit",
        "isApiErrorMessage": True,
        "quotaLimits": {
            "rateLimitType": "five_hour",
            "status": "rejected",
            "resetsAt": int(resets.timestamp()),
        },
        "sessionId": world.row(handle.run_id)["sessionId"],
    }
    world.write_line(handle.run_id, line)
    world.set_session(handle.run_id, status="idle", state="done", pid=None)
    world.tick(1)
    task = world.get(task_id)
    assert (task.ball, task.ball_reason) == (Ball.EXTERNAL, BallReason.SERVICE)
    assert pages(task) == []

    world.store_answers("ready")
    world.ticks(600, 3600, 2 * 3600 - 10)
    assert world.calls("nudges.log") == [], "nothing is sent while the limit is in force"
    world.reply_when_woken(handle.run_id, at=2 * 3600 + 70)
    world.ticks(2 * 3600 + 61, 2 * 3600 + 120, 2 * 3600 + 180)

    nudges = world.calls("nudges.log")
    assert len(nudges) == 1, "resumed once"
    assert "releases the merge gate" in nudges[0]["stdin"], "on its original envelope"
    assert len(world.calls("launches.log")) == 1
    task = world.get(task_id)
    assert task.ball is Ball.AGENT, "the service park is cleared by the recovery itself"
    assert world.person.actions == [] and pages(task) == []


# ===== a4: the regressions that must fail loudly ========================================


class TestRegressions:
    def test_a_disabled_recorded_runner_is_refused_rather_than_swapped(self, world: World) -> None:
        """Envelope drift, the hard half: the recorded runner can no longer run at all."""
        task_id = world.task()
        handle = world.dispatch(task_id, group="big-dawg", merge_mode=MergeMode.AUTOMERGE)
        world.configure(fable_enabled=False)
        _worker_gone(world, handle.run_id)
        world.tick(10)
        fire_retry(world, 20)
        fire_retry(world, 800)
        models = [launch["model"] for launch in world.calls("launches.log")]
        assert models == ["claude-fable-5-1"], "never the next member of the old group"
        task = world.get(task_id)
        assert task.ball is Ball.HUMAN
        prompt = task.ball_prompt or ""
        assert "policy_revoked" in prompt and "different runner" in prompt, prompt

    def test_a_fresh_execution_does_not_reset_the_lifetime_budget(self, world: World) -> None:
        from agentjobs.models_v2 import DispatchTrigger

        world.configure(limits={"auto": {"per_task_lifetime": 2}})
        task_id = world.task()
        handle = world.dispatch(task_id)
        _worker_gone(world, handle.run_id)
        world.tick(10)
        fire_retry(world, 20)
        assert len(world.calls("launches.log")) == 2
        [retry] = [r for r in world.rows() if r.get("state") != "stopped"]
        world.set_rows([dict(r, state="stopped", status="stopped", pid=None) for r in world.rows()])
        world.tick(800)
        fire_retry(world, 810)
        assert len(world.calls("launches.log")) == 2, "the cap binds the retry"
        assert "budget_exhausted" in (world.get(task_id).ball_prompt or "")

        # A person's answer, continued by the machine: a new execution id, the same budget.
        answer = _answer(world, task_id, "Try once more.")
        world.clock.move_to(2000)
        with pytest.raises(Exception, match="(?i)lifetime|budget|cap"):
            world.dispatch(
                task_id,
                request=DispatchRequest(
                    task_id=task_id, trigger=DispatchTrigger.AUTO, caused_by=answer
                ),
            )
        assert len(world.calls("launches.log")) == 2 and retry

    def test_two_projects_with_one_task_id_share_nothing_but_the_machine_slots(
        self, world: World
    ) -> None:
        world.add_project("other")
        world.configure(limits={"max_concurrent_runs": 2})
        mine = world.task()
        occupant = world.task(title="Occupies a slot")
        world.dispatch(occupant)
        theirs = world.task("other")
        assert theirs == mine, "one task id in two projects is the collision this is about"

        # Both race for the one remaining slot, from two processes at once.
        children = []
        for project_id, task_id in (("sandbox", mine), ("other", theirs)):
            caused = world.authorise(task_id, project_id)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                [str(SOURCE), str(TESTS), env.get("PYTHONPATH", "")]
            )
            env["AGENTJOBS_HOME"] = str(world.home)
            script = CHILD.replace(
                'project = registry.get("sandbox")', f'project = registry.get("{project_id}")'
            ).replace('manager=managers["sandbox"]', f'manager=managers["{project_id}"]')
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(world.home),
                        str(TESTS),
                        "dispatch",
                        "none",
                        world.clock.zero.isoformat(),
                        str(world.clock.offset),
                        f"{task_id}:{caused}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )
        outputs = [child.communicate(timeout=300) for child in children]
        store = journal(world.home)
        attempts = store.read(
            "SELECT run_id, project_id, task_id, state, takes_slot, slot_released_at, "
            "slot_released_reason, admitted_at, holder_pid, outcome, status, concluded_by, session_id "
            "FROM run_attempt ORDER BY admitted_at"
        )
        evidence = "\n".join(
            [repr(dict(row)) for row in attempts]
            + [
                f"child {i} exit {c.returncode}:\n{o}\n{e}"
                for i, (c, (o, e)) in enumerate(zip(children, outputs))
            ]
        )
        assert (
            len(world.calls("launches.log")) == 2
        ), f"exactly one remaining slot was awarded\n{evidence}"
        live = sorted((a.project_id, a.task_id) for a in store.live_attempts())
        assert len(live) == 2 and ("sandbox", occupant) in live

        winner_project, winner_task = next(item for item in live if item != ("sandbox", occupant))
        loser_project = "other" if winner_project == "sandbox" else "sandbox"
        assert (
            store.latest_execution(loser_project, winner_task) is None
        ), "nothing of the winner's execution is visible under the other project's id"

    def test_a_history_from_an_unknown_workflow_version_is_kept_and_never_acted_on(
        self, world: World
    ) -> None:
        import sqlite3

        from agentjobs.execution.factory import execution_db_path

        task_id = world.task()
        handle = world.dispatch(task_id)
        execution = journal(world.home).latest_execution("sandbox", task_id)
        assert execution is not None
        world.fresh_stores()
        with sqlite3.connect(execution_db_path(world.home)) as connection:
            connection.execute(
                "UPDATE execution SET workflow_version = 99, snapshot_json = NULL "
                "WHERE execution_id = ?",
                (execution.execution_id,),
            )
            connection.execute(
                "UPDATE execution_event SET payload_json = json_set(payload_json, "
                "'$.workflow_version', 99) WHERE execution_id = ? AND kind = 'accepted'",
                (execution.execution_id,),
            )
        _worker_gone(world, handle.run_id)
        before = len(world.get(task_id).log)
        lines = world.ticks(10, 20, 800, 1600)
        assert any("99" in line for line in lines), lines
        assert len(world.calls("launches.log")) == 1, "fails closed: no relaunch"
        assert len(world.get(task_id).log) == before, "and no task write"
        kept = journal(world.home).execution(execution.execution_id)
        assert kept is not None and not kept.terminal, "the history is kept, not rewritten"

    def test_a_controller_that_lost_ownership_cannot_record_a_stale_result(
        self, world: World
    ) -> None:
        from agentjobs.execution.errors import StaleOwner

        task_id = world.task()
        handle = world.dispatch(task_id)
        _worker_gone(world, handle.run_id)
        stale = Controller(world.home, managers=dict(world.managers))
        world.clock.move_to(10)
        stale.tick()  # concludes the attempt and holds the execution's epoch in memory
        execution = journal(world.home).latest_execution("sandbox", task_id)
        assert execution is not None
        held = stale._epochs[execution.execution_id]

        # Another process becomes the controller.
        world.clock.move_to(20)
        run_in_child(world, "controller", "none")
        taken = journal(world.home).execution(execution.execution_id)
        assert taken is not None and taken.controller_epoch > held

        # The result a stale controller computed before it lost ownership is refused.
        activity, _ = journal(world.home).record_intent(
            f"{execution.execution_id}:stale-probe",
            execution_id=execution.execution_id,
            kind="observe",
            input={"run_id": handle.run_id},
            owner_epoch=held,
        )
        with pytest.raises(StaleOwner):
            journal(world.home).record_result(
                activity.activity_id, state="applied", owner_epoch=held
            )
        with pytest.raises(StaleOwner):
            journal(world.home).close_execution(
                execution.execution_id, outcome="escalated", reason="stale", epoch=held
            )
        world.clock.move_to(900)
        stale.tick()
        fire_retry(world, 910)
        assert len(world.calls("launches.log")) == 2, "one relaunch, however many controllers"
        assert len(world.live_sessions(task_id)) == 1

    def test_grounding_outlives_two_real_supervisor_deaths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parent-grounding regression: a parked child still grounds takeoffs after the
        supervisor's process is killed, twice, and a fresh walk never starts the third."""
        from agentjobs.dispatch.epic import WalkStop
        from test_epic_supervision import WALK_CHILD, Epic
        from test_execution_controller import Machine, environment

        machine = Machine(tmp_path, monkeypatch)
        machine.configure(controller="shadow")
        walk = Epic(machine)
        first, second, third = walk.child("First"), walk.child("Second"), walk.child("Third")
        two_at_once = WALK_CHILD.replace("max_concurrent=1", "max_concurrent=2").replace(
            "epic.walk_epic(", "result = epic.walk_epic("
        )
        two_at_once += "\nprint('walk ended:', result and result.stop, result and result.detail)\n"
        assert two_at_once.count("max_concurrent=2") == 1

        def supervisor_dies() -> "subprocess.CompletedProcess[str]":
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    two_at_once,
                    str(machine.home),
                    walk.parent_id,
                    "none",
                    str(TESTS),
                ],
                capture_output=True,
                text=True,
                env=environment(machine.home),
                timeout=300,
            )

        died = supervisor_dies()  # takes off two children, then dies at its first wait
        assert died.returncode == 9, died.stderr
        assert len(walk.sessions_named(first)) == 1 and len(walk.sessions_named(second)) == 1
        machine.manager.handoff(
            first,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at this.",
        )
        died = supervisor_dies()  # a fresh supervisor reconciles, grounds, and dies again
        assert died.returncode == 9, (died.stdout, died.stderr)
        [record] = journal(machine.home).open_walks()
        assert record.grounding and record.grounding["stop"] == "child_needs_a_human"

        result = walk.walk(on_sleep=lambda _n: walk.complete_active(), max_concurrent=2)
        assert result is not None and result.stop is WalkStop.CHILD_NEEDS_A_HUMAN
        assert walk.sessions_named(third) == [], "no takeoff after grounding, after two deaths"
        landed = {a.child_id: a.verdict.value for a in result.attempts}
        assert landed == {first: "parked", second: "completed"}, "the flying sibling landed"
        assert walk.epic_attempts(first) == 1 and walk.epic_attempts(second) == 1


# ===== a5: a capability the real driver lacks is not lent to it by the fake ============


class TestCapabilities:
    def test_the_published_matrix_is_the_one_the_controller_uses(self) -> None:
        from agentjobs.dispatch.controller import CAPABILITIES, capabilities_for

        readme = (HERE / "README.md").read_text(encoding="utf-8")
        for (driver, mode), capability in CAPABILITIES.items():
            row = f"| {driver} | {mode} | `{capability.correlation}` | no |"
            assert row in readme, f"README's matrix is missing or wrong for {driver}/{mode}"
        batch = capabilities_for("claude", "batch")
        assert f"| any | batch | `{batch.correlation}` | no |" in readme
        assert not any(c.authoritative_absence for c in CAPABILITIES.values())

    def test_a_driver_without_correlation_is_effect_unknown_even_when_the_fake_could_list_it(
        self, world: World
    ) -> None:
        """The session exists and the fake CLI would list it by name. A Codex launch has no
        such listing, so the controller must not use one: unknown, escalated, not relaunched."""
        task_id = world.task()
        caused_by = world.authorise(task_id)
        die_in_child(world, "dispatch", "after_launch", f"{task_id}:{caused_by}")
        [attempt] = journal(world.home).live_attempts()
        RunDirectory(path=runs_root(world.home) / attempt.run_id).update_meta(driver="codex")
        assert len(world.live_sessions(task_id)) == 1

        world.ticks(10, 20)
        fire_retry(world, 30)
        fire_retry(world, 800)

        assert len(world.calls("launches.log")) == 1, "never a second launch on an unknown"
        still = journal(world.home).attempt(attempt.run_id)
        assert still is not None and still.is_live, "ownership kept while unknown"
        task = world.get(task_id)
        assert task.ball is Ball.HUMAN and "effect_unknown" in (task.ball_prompt or "")
        assert "codex" in (task.ball_prompt or "").lower() or any(
            "codex" in (e.body or "").lower() for e in task.log
        )
        assert len(pages(task)) == 1, "one escalation, however many ticks"

    def test_an_unknown_launch_keeps_its_ownership_when_another_dispatch_runs(
        self, world: World
    ) -> None:
        """Found by this harness: any later admission on the machine used to release an
        `effect_unknown` attempt as "never launched", freeing the task for a second writer."""
        task_id = world.task()
        caused_by = world.authorise(task_id)
        die_in_child(world, "dispatch", "after_marker", f"{task_id}:{caused_by}")
        # Past the reconcile deadline, because a launch is only `effect_unknown` once it
        # has had its 600 seconds to turn up in a listing. This used to say `20` and pass,
        # and the reason is task-518's whole subject: the child stamped its admission on
        # the machine's clock and the harness read one two hours ahead, so *every* attempt
        # was two hours old on its first tick and the deadline was never actually tested.
        # Two ticks past it, because the tick that classifies the launch `effect_unknown`
        # is not the one that hands it to a person.
        world.ticks(10, 620, 621)
        [attempt] = journal(world.home).live_attempts()
        assert "effect_unknown" in (world.get(task_id).ball_prompt or "")

        world.dispatch(world.task(title="Unrelated"))
        world.fresh_stores()
        DispatchLedger(world.home, managers=dict(world.managers)).reconcile()

        still = journal(world.home).attempt(attempt.run_id)
        assert still is not None and still.is_live, "released on no evidence"
        from agentjobs.dispatch.guards import LiveRunExistsError

        with pytest.raises(LiveRunExistsError):
            world.dispatch(task_id)

    def test_a_claude_launch_marked_but_never_listed_is_unknown_not_absent(
        self, world: World
    ) -> None:
        task_id = world.task()
        caused_by = world.authorise(task_id)
        die_in_child(world, "dispatch", "after_marker", f"{task_id}:{caused_by}")
        assert world.rows() == []
        world.ticks(10, 20)
        fire_retry(world, 30)
        assert world.calls("launches.log") == [], "absence from the listing proves nothing"
        task = world.get(task_id)
        assert "effect_unknown" in (task.ball_prompt or "")


@pytest.mark.skipif(
    os.environ.get("AGENTJOBS_REAL_DRIVER_CONTRACT") != "1",
    reason="reads the installed Claude CLI's session listing; opt in with "
    "AGENTJOBS_REAL_DRIVER_CONTRACT=1",
)
class TestTheRealDriverContract:
    """Isolated and read-only: no launch, no stop, no probe, nothing near a credential."""

    def test_the_real_listing_has_every_field_the_fake_and_the_controller_rely_on(
        self, world: World
    ) -> None:
        import shutil

        executable = shutil.which("claude")
        assert executable, "the Claude CLI is not on PATH"
        # The world points CLAUDE_CONFIG_DIR at an empty directory so that nothing else in
        # this file can reach the real store. This one read-only listing needs the machine's.
        real_env = {k: v for k, v in os.environ.items() if k != CLAUDE_HOME_ENV}
        done = subprocess.run(
            [executable, "agents", "--json", "--all"],
            env=real_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        rows = json.loads(done.stdout)
        assert isinstance(rows, list)
        task_id = world.task()
        world.dispatch(task_id)
        fake_keys = set(world.rows()[0])
        background = [row for row in rows if row.get("kind") == "background"]
        assert background, "no background session to compare against"
        for row in background:
            assert {"id", "sessionId", "cwd", "kind", "state"} <= set(row), row
        named = [row for row in background if row.get("name")]
        assert named, "session_name correlation needs a listed name"
        assert {"id", "sessionId", "cwd", "kind", "name", "state"} <= fake_keys


# ===== a7: the rollup counts exactly what the harness injected ==========================


def test_the_failure_rollup_reports_every_injected_class_with_its_count(world: World) -> None:
    from collections import Counter

    from typer.testing import CliRunner

    from agentjobs.cli import app
    from agentjobs.dispatch.finish import FLAKY_TEST, FinishDirectory, GateAttempt, GateVerdict
    from agentjobs.dispatch.phases import record_phase

    injected: Counter = Counter()
    actions: Counter = Counter()

    # worker_gone, retried.
    gone = world.task(title="Loses its worker")
    handle = world.dispatch(gone)
    _worker_gone(world, handle.run_id)
    world.tick(10)
    fire_retry(world, 20)
    injected["worker_gone"] += 1

    # effect_unknown, escalated: one human action.
    unknown = world.task(title="Launch unknown")
    caused_by = world.authorise(unknown)
    die_in_child(world, "dispatch", "after_marker", f"{unknown}:{caused_by}")
    world.ticks(760, 770)
    injected["effect_unknown"] += 1
    actions["effect_unknown"] += 1

    # auth_unavailable on a dead store, notified: one login.
    login = world.task(title="Login expires")
    parked = world.dispatch(login)
    world.stall(parked.run_id, at=1000)
    for moment in range(1000, 1400, 60):
        world.tick(moment)
    assert len(pages(world.get(login))) == 1
    world.store_answers("ready")
    world.reply_when_woken(parked.run_id, at=1450)
    world.ticks(1440, 1500)
    injected["auth_unavailable"] += 1
    actions["auth_unavailable"] += 1

    # cancelled_by_user.
    stopped = world.task(title="Stopped")
    doomed = world.dispatch(stopped)
    world.clock.move_to(1600)
    assert world.stop(doomed.run_id).stopped
    injected["cancelled_by_user"] += 1

    # flaky_test twice on one test id, and one browser death, recorded the way the
    # finisher and the gate record them.
    for number in range(2):
        directory = FinishDirectory.create(world.home, gone, "sandbox")
        red = GateAttempt(
            1,
            [],
            "a" * 40,
            "b" * 40,
            None,
            ok=False,
            code=1,
            stage="pytest",
            tests=["FAILED tests/test_flaky.py::test_sometimes - boom"],
        )
        green = GateAttempt(2, ["--from", "pytest"], "a" * 40, "b" * 40, None, ok=True)
        directory.record(
            "finish_gate_receipt", **GateVerdict([red, green], classification=FLAKY_TEST).data()
        )
        injected["flaky_test"] += 1
    record_phase(
        runs_root(world.home) / handle.run_id,
        "gate_stage_browser_gone",
        stage="e2e",
        tests=["e2e/x.spec.ts:1 › a page"],
        retried=True,
        passed=True,
    )
    injected["browser_death"] += 1

    world.fresh_stores()
    result = CliRunner().invoke(
        app, ["execution", "failures", "--json"], env={"AGENTJOBS_HOME": str(world.home)}
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    by_class = {item["class"]: item for item in report["classes"]}
    assert {name: item["count"] for name, item in by_class.items()} == dict(injected)
    assert {n: i["human_actions"] for n, i in by_class.items() if i["human_actions"]} == dict(
        actions
    )
    assert by_class["worker_gone"]["dispositions"]["retried"] == 1
    assert by_class["effect_unknown"]["dispositions"]["stopped"] == 1
    assert by_class["flaky_test"]["repeated_tests"] == ["tests/test_flaky.py::test_sometimes"]
    assert by_class["browser_death"]["tests"] == {"e2e/x.spec.ts:1 › a page": 1}
    assert handle.run_id in by_class["worker_gone"]["runs"]
    assert f"sandbox/{login}" in by_class["auth_unavailable"]["tasks"]
