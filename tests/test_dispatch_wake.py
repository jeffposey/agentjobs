"""Resuming the session that already has the context, instead of booting one that has not.

The through-line: **a wake is an optimisation and never a precondition.** Every one of
these tests exists in a pair -- what waking does when it can, and that dispatch still
starts a cold session when it cannot. A bug here that makes waking impossible costs
eleven minutes; a bug that makes dispatch *fail* costs the run, so the fallbacks get as
much attention as the happy path.

Two properties are load-bearing and would otherwise fail silently, so they are tested
directly rather than through their consequences:

- **The prompt goes on stdin, never in argv.** ``--remote-control`` and ``--resume``
  together drop a positional prompt and leave the session idle with its conversation
  restored, which dispatch reads as ``FINISHED``. A regression here looks like an agent
  that stopped without handing off.
- **The session lookup passes ``--all``.** Without it the listing is active-only and a
  stopped session -- the entire population a wake looks at -- is not in it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchLimits,
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    PostureSource,
    ProjectDispatchSettings,
    ResolvedPosture,
    RunnerMode,
)
from agentjobs.dispatch.peers import SESSIONS_DIR_ENV, LiveSession, PeerDelivery
from agentjobs.dispatch.runner import DispatchRunner, RunDirectory
from agentjobs.dispatch.wake import (
    WAKE_PATH_FORK,
    WAKE_PATH_IN_PLACE,
    WakeError,
    WakeTarget,
    build_wake_prompt,
    find_wake_target,
    resume_refusal,
    session_uuids,
    wake_argv,
    wake_in_place,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger, Lifecycle
from support import task_store

# ----- a launcher that records what it was given ------------------------------

FAKE_CLI = """
import json, sys, pathlib
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args[:2] == ["agents", "--json"]:
    rows = json.loads((here / "sessions.json").read_text())
    if "--all" not in args:
        # The real CLI prints *active* sessions unless --all is passed, and a stopped
        # session has no `status`. Modelling that is the point of this fake: a wake that
        # forgot --all must fail here rather than in production.
        rows = [r for r in rows if r.get("status")]
    print(json.dumps(rows))
    raise SystemExit(0)
if args[:1] == ["-p"]:
    # The peer-channel sender (task-451). It is the same executable as the launcher, so
    # the fake has to answer as both; `send_outcome.txt` is how a test makes it refuse.
    (here / "sent.txt").write_text(sys.stdin.read() if not sys.stdin.isatty() else "")
    outcome = here / "send_outcome.txt"
    print(outcome.read_text() if outcome.exists() else "AGENTJOBS-WAKE-DELIVERED")
    raise SystemExit(0)
(here / "argv.json").write_text(json.dumps(args))
(here / "stdin.txt").write_text(sys.stdin.read() if not sys.stdin.isatty() else "")
print("backgrounded \\u00b7 feed1234 \\u00b7 a name")
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "project").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "tasks").mkdir()
    return tmp_path


@pytest.fixture
def manager(workspace: Path) -> TaskManager:
    return TaskManager(task_store(workspace / "tasks"))


@pytest.fixture
def task(manager: TaskManager):
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return manager.claim_task(created.id, agent="claude")


@pytest.fixture
def cli(workspace: Path) -> Path:
    script = workspace / "fakecli.py"
    script.write_text(textwrap.dedent(FAKE_CLI), encoding="utf-8")
    (workspace / "sessions.json").write_text("[]", encoding="utf-8")
    return script


def set_sessions(workspace: Path, rows: List[dict]) -> None:
    (workspace / "sessions.json").write_text(json.dumps(rows), encoding="utf-8")


def stopped_row(short: str, uuid: str) -> dict:
    """A session the manager still holds but is not running -- no ``status``, as observed."""
    return {"id": short, "sessionId": uuid, "kind": "background", "state": "done"}


def build(
    workspace: Path,
    manager: TaskManager,
    cli: Path,
    *,
    resume_sessions: bool = True,
    posture: Optional[ResolvedPosture] = None,
) -> DispatchRunner:
    runner = RunnerConfig(
        name="fake",
        argv=[sys.executable, str(cli), "--bg", "--remote-control", "{prompt}"],
        env={},
        mode=RunnerMode.SESSION,
    )
    settings = ProjectDispatchSettings(
        project_id="sandbox",
        enabled=True,
        runner="fake",
        require_clean_tree=False,
        posture=Posture.AUTO,
        resume_sessions=resume_sessions,
    )
    limits = DispatchLimits()
    return DispatchRunner(
        manager=manager,
        resolution=DispatchResolution(
            project_id="sandbox",
            runner=runner,
            settings=settings,
            limits=limits,
            config=DispatchConfig(enabled=True, limits=limits),
        ),
        project_root=workspace / "project",
        home=workspace / "home",
        api_base="http://localhost:8899",
        posture=posture,
    )


def seed_finished_run(
    home: Path,
    task_id: str,
    *,
    run_id: str = "run_previous",
    session_id: str = "aaaa1111",
    status: str = "finished",
    started_at: str = "2026-08-20T08:00:00+00:00",
    reaped: bool = False,
    posture: Optional[str] = "auto",
) -> RunDirectory:
    meta: Dict[str, object] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": "sandbox",
        "mode": "session",
        "posture": posture,
        "status": status,
        "session_id": session_id,
        "started_at": started_at,
        "dispatch_entry_id": 3,
    }
    if reaped:
        meta["reaped"] = True
    return RunDirectory.create(home, run_id, meta)


# ----- the live-session roster (task-451) -------------------------------------


def roster_dir() -> Path:
    """The empty directory ``conftest.isolate_session_roster`` points every test at."""
    return Path(os.environ[SESSIONS_DIR_ENV])


def register_live(
    *,
    session_uuid: str,
    name: str = "sandbox/task-001/previous",
    pid: int = 4242,
    status: str = "idle",
) -> None:
    """Put one live registration in front of the wake, shaped as Claude Code writes them."""
    (roster_dir() / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_uuid,
                "jobId": session_uuid[:8],
                "cwd": "\\\\projects\\\\agentjobs",
                "kind": "bg",
                "version": "2.1.276",
                "status": status,
                "name": name,
                "peerProtocol": 1,
                "peerFeatures": ["notify_idle", "artifact_yield"],
                "messagingSocketPath": "\\\\\\\\.\\\\pipe\\\\LOCAL\\\\cc-msg-deadbeef",
            }
        ),
        encoding="utf-8",
    )


def refuse_sends(workspace: Path, why: str = "it was held for approval") -> None:
    """Make the fake sender report that SendMessage did not deliver."""
    (workspace / "send_outcome.txt").write_text(f"AGENTJOBS-WAKE-FAILED {why}", encoding="utf-8")


def sent_instruction(workspace: Path) -> Optional[str]:
    """What the peer sender was asked to deliver, or ``None`` if it was never run."""
    path = workspace / "sent.txt"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def run_meta(workspace: Path, run_id: str) -> Dict[str, object]:
    """One run's recorded meta, by run id."""
    path = workspace / "home" / "runs" / run_id / "meta.yaml"
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def ran_argv(workspace: Path) -> List[str]:
    loaded = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    return [str(element) for element in loaded]


def ran_stdin(workspace: Path) -> str:
    return (workspace / "stdin.txt").read_text(encoding="utf-8")


# ----- choosing what to resume ------------------------------------------------


class TestFindWakeTarget:
    def test_a_task_with_no_previous_session_has_nothing_to_wake(self, workspace: Path) -> None:
        assert (
            find_wake_target(workspace / "home", "task-001", project_id="sandbox", rows=[]) is None
        )

    def test_a_finished_session_still_in_the_ledger_is_the_target(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        target = find_wake_target(
            home, "task-001", project_id="sandbox", rows=[stopped_row("aaaa1111", "aaaa1111-u")]
        )

        assert target is not None
        assert target.previous_run_id == "run_previous"
        assert target.session_uuid == "aaaa1111-u"

    def test_the_full_uuid_is_carried_not_the_short_id(self, workspace: Path) -> None:
        """``--resume`` takes the UUID. Handing it the short id is a resume that fails."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        target = find_wake_target(
            home,
            "task-001",
            project_id="sandbox",
            rows=[stopped_row("aaaa1111", "aaaa1111-2222-3333-4444-555555555555")],
        )

        assert target is not None
        assert target.session_uuid == "aaaa1111-2222-3333-4444-555555555555"
        assert target.session_uuid != target.session_id

    def test_a_live_run_is_not_woken(self, workspace: Path) -> None:
        """Something is already working it; whether to dispatch at all is the lock's call."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", status="running")

        assert (
            find_wake_target(
                home, "task-001", project_id="sandbox", rows=[stopped_row("aaaa1111", "u")]
            )
            is None
        )

    def test_a_reaped_run_is_not_woken(self, workspace: Path) -> None:
        """``claude rm`` deleted the conversation; the run's own meta says so."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", reaped=True)

        assert (
            find_wake_target(
                home, "task-001", project_id="sandbox", rows=[stopped_row("aaaa1111", "u")]
            )
            is None
        )

    def test_a_session_the_manager_no_longer_lists_is_not_woken(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        assert (
            find_wake_target(
                home, "task-001", project_id="sandbox", rows=[stopped_row("bbbb2222", "u")]
            )
            is None
        )

    def test_only_the_newest_run_is_ever_a_candidate(self, workspace: Path) -> None:
        """The rule that stops a stale conversation being resumed.

        Two finished runs for one task, and the newer one's session has been deleted.
        Falling back to the older one would hand the human an agent whose picture of the
        branch is a whole run out of date -- worse than the cold start it avoided -- so
        the answer is None, not the survivor.
        """
        home = workspace / "home"
        seed_finished_run(
            home,
            "task-001",
            run_id="run_older",
            session_id="old00001",
            started_at="2026-08-20T08:00:00+00:00",
        )
        seed_finished_run(
            home,
            "task-001",
            run_id="run_newer",
            session_id="new00001",
            started_at="2026-08-21T08:00:00+00:00",
        )

        target = find_wake_target(
            home, "task-001", project_id="sandbox", rows=[stopped_row("old00001", "old-uuid")]
        )

        assert target is None

    def test_another_tasks_session_is_never_borrowed(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-999", session_id="aaaa1111")

        assert (
            find_wake_target(
                home, "task-001", project_id="sandbox", rows=[stopped_row("aaaa1111", "u")]
            )
            is None
        )


class TestSessionUuids:
    def test_rows_without_both_ids_are_dropped(self) -> None:
        mapping = session_uuids(
            [
                {"id": "aaaa1111", "sessionId": "full-a"},
                {"id": "bbbb2222"},
                {"sessionId": "full-c"},
                {"id": "", "sessionId": "full-d"},
                "not a mapping",  # type: ignore[list-item]
            ]
        )

        assert mapping == {"aaaa1111": "full-a"}


# ----- rewriting the argv -----------------------------------------------------


class TestWakeArgv:
    def test_the_prompt_element_becomes_a_resume(self) -> None:
        argv = ["claude", "--bg", "--remote-control", "--permission-mode", "auto", "the prompt"]

        assert wake_argv(argv, "the prompt", "u-u-i-d") == [
            "claude",
            "--bg",
            "--remote-control",
            "--permission-mode",
            "auto",
            "--resume",
            "u-u-i-d",
        ]

    def test_the_prompt_is_not_left_anywhere_in_the_argv(self) -> None:
        """The anti-regression test for the silent failure this whole design turns on.

        ``--remote-control`` with ``--resume`` drops a positional prompt without saying
        so: the session comes up with its conversation restored, its prompt box empty,
        and settles at ``idle`` -- which ``classify_session`` calls ``FINISHED``. So a
        wake that left the prompt in argv would be reported as an agent that finished
        without handing off, on every task, and nothing in the output would say why.
        """
        argv = ["claude", "--bg", "--remote-control", "carry me"]

        assert "carry me" not in wake_argv(argv, "carry me", "u")

    def test_posture_flags_and_the_model_survive_untouched(self) -> None:
        """A wake and a cold start must not drift apart in what the run is allowed to do."""
        argv = ["claude", "--bg", "--model", "claude-opus-5", "--settings", "{json}", "prompt"]

        assert wake_argv(argv, "prompt", "u")[:6] == argv[:6]

    def test_an_argv_that_does_not_carry_the_prompt_refuses(self) -> None:
        with pytest.raises(WakeError):
            wake_argv(["claude", "--bg"], "prompt", "u")

    def test_only_the_first_match_is_replaced(self) -> None:
        """One resume, not one per element that happens to contain the text."""
        result = wake_argv(["claude", "x", "x"], "x", "u")

        assert result.count("--resume") == 1
        assert result == ["claude", "--resume", "u", "x"]


# ----- what the woken session is told -----------------------------------------


class TestWakePrompt:
    def test_the_ball_prompt_rides_verbatim(self) -> None:
        rendered = build_wake_prompt(
            agent="claude",
            task_id="task-001",
            ball_prompt="Approved -- cleared to merge.",
            api_base="http://localhost:8899",
            run_id="run_new",
            previous_run_id="run_old",
        )

        assert "Approved -- cleared to merge." in rendered
        assert "task-001" in rendered
        assert "run_old" in rendered

    def test_it_says_this_is_the_same_session(self) -> None:
        """A resumed agent that thinks it is new takes a second worktree and starts over."""
        rendered = build_wake_prompt(
            agent="claude",
            task_id="task-001",
            ball_prompt="go",
            api_base="b",
            run_id="r",
            previous_run_id="p",
        )

        assert "same session" in rendered
        assert "do not take a second worktree" in rendered.lower()

    def test_it_tells_a_confused_agent_to_hand_back_rather_than_improvise(self) -> None:
        rendered = build_wake_prompt(
            agent="claude",
            task_id="task-001",
            ball_prompt="go",
            api_base="b",
            run_id="r",
            previous_run_id="p",
        )

        assert "hand the ball back" in rendered

    def test_a_missing_ball_prompt_still_produces_an_instruction(self) -> None:
        rendered = build_wake_prompt(
            agent="claude",
            task_id="task-001",
            ball_prompt="",
            api_base="b",
            run_id="r",
            previous_run_id="p",
        )

        assert "newest handoff" in rendered

    def test_a_runaway_ball_prompt_is_truncated_and_says_so(self) -> None:
        rendered = build_wake_prompt(
            agent="claude",
            task_id="task-001",
            ball_prompt="x" * 20000,
            api_base="b",
            run_id="r",
            previous_run_id="p",
        )

        assert "truncated" in rendered
        assert len(rendered) < 20000


# ----- the whole path, through a real spawn -----------------------------------


class TestDispatchWakes:
    def test_a_second_dispatch_resumes_the_first_session(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "aaaa1111-full-uuid")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        argv = ran_argv(workspace)
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "aaaa1111-full-uuid"

    def test_the_prompt_is_delivered_on_stdin_and_is_absent_from_argv(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """Both halves in one test, because either alone would pass a broken wake.

        Asserting only "stdin has the prompt" passes an argv that also carries it;
        asserting only "argv does not" passes a wake that delivers nothing at all and
        parks forever.
        """
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert "same session" in ran_stdin(workspace)
        assert not any("same session" in element for element in ran_argv(workspace))

    def test_the_run_records_what_it_resumed(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """`run_report.py` reads this to tell a woken run from a cold one."""
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        meta = yaml.safe_load((handle.directory.path / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["resumed"] is True
        assert meta["resumed_from"] == "run_previous"
        assert meta["resumed_session"] == "u-u-i-d"

    def test_the_task_record_says_the_session_was_resumed(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        updated = manager.get_task(task.id)
        assert updated is not None
        assert "Resumed the session from run `run_previous`" in (updated.log[-1].body or "")

    def test_the_session_lookup_asks_for_finished_sessions(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """Without ``--all`` the listing is active-only and every wake target is invisible.

        The fake CLI filters out rows with no ``status`` when ``--all`` is absent, which
        is what the real one does -- measured on 2.1.238, where ``agents --json --cwd``
        returned zero rows for a stopped session and ``--json --all --cwd`` returned it.
        So dropping the flag makes this test fail by finding nothing to resume, which is
        exactly how it would fail in production.
        """
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert "--resume" in ran_argv(workspace)


class TestDispatchStartsColdInstead:
    """Every way a wake can be unavailable, and dispatch working anyway.

    These are the tests that matter most. A wake that does not happen costs eleven
    minutes; a dispatch that refuses because a wake was unavailable costs the run.
    """

    def test_a_first_dispatch_is_a_cold_start(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        argv = ran_argv(workspace)
        assert "--resume" not in argv
        assert any(task.id in element for element in argv), "the cold prompt rides in argv"

    def test_a_cold_start_leaves_stdin_alone(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """The existing path is untouched: its prompt is in argv and nothing is piped."""
        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert ran_stdin(workspace) == ""

    def test_resume_sessions_off_starts_cold(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli, resume_sessions=False).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert "--resume" not in ran_argv(workspace)

    def test_a_deleted_conversation_starts_cold(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        set_sessions(workspace, [])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert "--resume" not in ran_argv(workspace)

    def test_an_unreadable_session_ledger_starts_cold_rather_than_failing(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """A runner that does not answer ``agents --json`` still dispatches.

        This is the asymmetry the whole design rests on, so it is asserted rather than
        argued: the ledger read is broken here in the crudest available way, and the run
        still starts.
        """
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111")
        (workspace / "sessions.json").write_text("not json at all", encoding="utf-8")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.MANUAL
        )

        assert handle.session_id == "feed1234"
        assert "--resume" not in ran_argv(workspace)


# ----- posture across a resume (task-375) -------------------------------------

AUTONOMOUS_FROM_EPIC = ResolvedPosture(
    posture=Posture.AUTONOMOUS, source=PostureSource.EPIC, ceiling=Posture.AUTONOMOUS
)


def newest_entry(manager: TaskManager, task_id: str):
    updated = manager.get_task(task_id)
    assert updated is not None
    return updated.log[-1]


class TestResumeRefusal:
    def test_the_same_posture_may_be_resumed(self) -> None:
        assert resume_refusal({"posture": "auto"}, "auto") is None

    def test_a_different_posture_is_refused_and_names_both(self) -> None:
        refusal = resume_refusal({"posture": "auto"}, "autonomous")
        assert refusal is not None
        assert "`auto`" in refusal and "`autonomous`" in refusal

    def test_an_unrecorded_posture_cannot_be_shown_to_match(self) -> None:
        assert resume_refusal({}, "auto") is not None


class TestPostureAcrossAResume:
    """task-358's child task-273: recorded ``autonomous``, only ever told ``auto``."""

    def test_a_session_last_run_at_auto_is_not_resumed_at_autonomous(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture="auto")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        handle = build(workspace, manager, cli, posture=AUTONOMOUS_FROM_EPIC).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.CHILD
        )

        argv = ran_argv(workspace)
        assert "--resume" not in argv
        assert ran_stdin(workspace) == ""
        assert any("releases the merge gate" in element for element in argv)
        meta = handle.directory.read_meta()
        assert "posture `auto`" in str(meta["resume_refused"])
        assert "resumed" not in meta

    def test_the_record_says_why_and_what_the_fresh_session_was_told(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture="auto")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli, posture=AUTONOMOUS_FROM_EPIC).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.CHILD
        )

        entry = newest_entry(manager, task.id)
        data = dict(entry.data or {})
        assert data["posture"] == "autonomous"
        delivery = data["delivery"]
        assert delivery["channel"] == "argv"
        assert delivery["posture_delivered"] is True
        assert delivery["acknowledged_by"] == "feed1234"
        assert "run_previous" in delivery["resume_refused"]
        assert "Started a fresh session" in (entry.body or "")
        assert "Resumed" not in (entry.body or "")

    def test_a_same_posture_wake_carries_the_posture_clause_on_stdin(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture="autonomous")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli, posture=AUTONOMOUS_FROM_EPIC).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.CHILD
        )

        stdin = ran_stdin(workspace)
        assert "--resume" in ran_argv(workspace)
        assert "same session" in stdin
        assert "releases the merge gate" in stdin
        delivery = dict(newest_entry(manager, task.id).data or {})["delivery"]
        assert delivery["channel"] == "stdin"
        assert delivery["posture_delivered"] is True
        assert delivery["payload_sha256"] == hashlib.sha256(stdin.encode("utf-8")).hexdigest()
        assert "resume_refused" not in delivery

    def test_an_auto_wake_is_told_auto_again(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """The defect's other half: a wake used to carry no posture clause at all."""
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture="auto")
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "Do not merge." in ran_stdin(workspace)
        delivery = dict(newest_entry(manager, task.id).data or {})["delivery"]
        assert delivery["posture_delivered"] is True

    def test_a_previous_run_with_no_recorded_posture_starts_cold(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture=None)
        set_sessions(workspace, [stopped_row("aaaa1111", "u-u-i-d")])

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "--resume" not in ran_argv(workspace)


# ----- waking a session that is still running (task-451) ----------------------


class TestWakeInPlaceUnit:
    """The decision on its own: roster lookup, then delivery, and every doubt is a fork."""

    TARGET = WakeTarget(
        previous_run_id="run_previous",
        session_id="aaaa1111",
        session_uuid="aaaa1111-2222-3333-4444-555555555555",
    )

    def test_a_session_that_is_not_in_the_roster_is_gone(self, tmp_path: Path) -> None:
        """The fallback trigger. Both files vanish within seconds of the process ending,
        so this is a file lookup and never a connect timeout."""
        outcome = wake_in_place(self.TARGET, "wake up", send=_unused, sessions_dir=tmp_path)

        assert not outcome
        assert "not in the live roster" in outcome.detail

    def test_a_live_session_is_sent_the_message(self, tmp_path: Path) -> None:
        _register(tmp_path, self.TARGET.session_uuid)
        sent: List[Tuple[str, str]] = []

        def send(live: LiveSession, text: str) -> PeerDelivery:
            sent.append((live.name, text))
            return PeerDelivery(True, "delivered")

        outcome = wake_in_place(self.TARGET, "wake up", send=send, sessions_dir=tmp_path)

        assert outcome
        assert sent == [("sandbox/task-001/previous", "wake up")]
        assert outcome.session is not None and outcome.session.pid == 4242

    def test_a_refused_delivery_is_not_a_wake(self, tmp_path: Path) -> None:
        """A held message is the failure mode worth naming: the session is alive, nothing
        errored, and the turn never arrived. Believing it would strand a supervisor."""
        _register(tmp_path, self.TARGET.session_uuid)

        outcome = wake_in_place(
            self.TARGET,
            "wake up",
            send=lambda live, text: PeerDelivery(False, "it was held"),
            sessions_dir=tmp_path,
        )

        assert not outcome
        assert "held" in outcome.detail

    def test_a_sender_that_raises_is_a_fork_rather_than_a_failed_dispatch(
        self, tmp_path: Path
    ) -> None:
        _register(tmp_path, self.TARGET.session_uuid)

        def boom(live: LiveSession, text: str) -> PeerDelivery:
            raise RuntimeError("the machine caught fire")

        outcome = wake_in_place(self.TARGET, "wake up", send=boom, sessions_dir=tmp_path)

        assert not outcome
        assert "the machine caught fire" in outcome.detail

    def test_the_uuid_is_rechecked_against_a_live_row(self, tmp_path: Path) -> None:
        """A name is not unique and the session ledger lists conversations with no process.

        Matching the uuid against a *live* row is what makes "this name belongs to the
        conversation I mean" a fact rather than a hope.
        """
        _register(tmp_path, "bbbb2222-0000-0000-0000-000000000000", name="somebody/else/1")

        assert not wake_in_place(self.TARGET, "wake up", send=_unused, sessions_dir=tmp_path)


def _unused(live: LiveSession, text: str) -> PeerDelivery:
    raise AssertionError("the sender should not have been reached")


def _register(directory: Path, session_uuid: str, name: str = "sandbox/task-001/previous") -> None:
    (directory / "4242.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "sessionId": session_uuid,
                "jobId": session_uuid[:8],
                "status": "idle",
                "name": name,
                "version": "2.1.276",
            }
        ),
        encoding="utf-8",
    )


class TestWakeInPlaceThroughDispatch:
    """The same decision where it is made, with a launcher that records what it was given.

    The pair every test here is built on: **the peer sender ran and the launcher did not**,
    or the other way round. ``argv.json`` exists only when something was launched, so its
    absence is the strongest available evidence that no second session was started.
    """

    def _previous(self, workspace: Path, task_id: str) -> None:
        seed_finished_run(workspace / "home", task_id, session_id="aaaa1111")
        set_sessions(workspace, [stopped_row("aaaa1111", "aaaa1111-uuid")])

    def test_a_live_session_is_woken_where_it_stands(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """ac-1, in the unit. The live half is on the task record."""
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert not (workspace / "argv.json").exists()
        assert "same session" in (sent_instruction(workspace) or "")
        assert handle.session_id == "aaaa1111"
        assert run_meta(workspace, handle.run_id)["wake_path"] == WAKE_PATH_IN_PLACE

    def test_the_run_adopts_the_session_rather_than_minting_one(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """The whole benefit, stated as an assertion.

        ``feed1234`` is what the fake launcher prints, so a run carrying it is a run that
        forked. A woken run carries the id the session has always had, which is what the
        poller, ``stop`` and reconciliation all follow.
        """
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert handle.session_id == "aaaa1111"
        assert handle.session_id != "feed1234"

    def test_the_dispatch_entry_says_the_message_went_by_the_peer_channel(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        entry = newest_entry(manager, task.id)
        delivery = dict(entry.data or {})["delivery"]
        assert delivery["channel"] == "peer"
        assert delivery["acknowledged_by"] == "aaaa1111"
        assert delivery["posture_delivered"] is True
        assert "in place" in (entry.body or "")

    def test_a_session_that_has_ended_forks_exactly_as_before(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """ac-2. Nothing in the roster, so nothing changed about the wake that existed."""
        self._previous(workspace, task.id)

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "--resume" in ran_argv(workspace)
        assert ran_argv(workspace)[ran_argv(workspace).index("--resume") + 1] == "aaaa1111-uuid"
        assert "same session" in ran_stdin(workspace)
        assert sent_instruction(workspace) is None
        meta = run_meta(workspace, handle.run_id)
        assert meta["wake_path"] == WAKE_PATH_FORK
        assert "not in the live roster" in str(meta["wake_detail"])

    def test_a_name_the_peer_channel_refuses_forks(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """ac-4. Every run dispatched before the separator changed is in this case."""
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid", name="sandbox/task-001@previous")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "--resume" in ran_argv(workspace)
        assert sent_instruction(workspace) is None
        assert "@" in str(run_meta(workspace, handle.run_id)["wake_detail"])

    def test_a_sender_that_reports_a_failure_forks(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """ac-4. The session is alive and reachable and the message still did not land."""
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")
        refuse_sends(workspace)

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert sent_instruction(workspace) is not None
        assert "--resume" in ran_argv(workspace)
        assert run_meta(workspace, handle.run_id)["wake_path"] == WAKE_PATH_FORK

    def test_a_sender_that_says_nothing_recognisable_forks(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")
        (workspace / "send_outcome.txt").write_text("I think that went fine!", encoding="utf-8")

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "--resume" in ran_argv(workspace)

    def test_a_cold_start_never_touches_the_peer_channel(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """No previous session means no conversation to wake, in place or otherwise."""
        register_live(session_uuid="aaaa1111-uuid")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert sent_instruction(workspace) is None
        assert "--resume" not in ran_argv(workspace)
        assert "wake_path" not in run_meta(workspace, handle.run_id)

    def test_a_woken_run_claims_no_launch(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """task-416's receipt stays honest: nothing was launched, so nothing says one was.

        A coordinator reads ``launch_attempted_at`` with no session id as "the launcher
        ran and nobody recorded what it printed". An in-place wake has a session id from
        its first write and never ran a launcher, so writing the marker would invent a
        launch for it to reconcile.
        """
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")

        handle = build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        meta = run_meta(workspace, handle.run_id)
        assert "launch_attempted_at" not in meta
        assert meta["session_name"] == f"sandbox/{task.id}/{handle.run_id[len('run_'):]}"
        assert meta["session_id"] == "aaaa1111"

    def test_the_posture_clause_still_reaches_a_session_woken_in_place(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """task-375's rule does not get a hole cut in it by a new delivery channel."""
        self._previous(workspace, task.id)
        register_live(session_uuid="aaaa1111-uuid")

        build(workspace, manager, cli).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.AUTO
        )

        assert "Do not merge." in (sent_instruction(workspace) or "")

    def test_a_posture_change_still_refuses_to_resume_at_all(
        self, workspace: Path, manager: TaskManager, task, cli: Path
    ) -> None:
        """The in-place path sits *behind* ``resume_refusal``, not beside it.

        A conversation remembers every clause it was told, so raising its authority by
        message would be exactly the authorisation change task-375 refuses -- and cheaper
        to do, which is the reason to check it.
        """
        seed_finished_run(workspace / "home", task.id, session_id="aaaa1111", posture="auto")
        set_sessions(workspace, [stopped_row("aaaa1111", "aaaa1111-uuid")])
        register_live(session_uuid="aaaa1111-uuid")

        build(workspace, manager, cli, posture=AUTONOMOUS_FROM_EPIC).start(
            task, actor="Jeff Posey", caused_by=1, trigger=DispatchTrigger.CHILD
        )

        assert sent_instruction(workspace) is None
        assert "--resume" not in ran_argv(workspace)
