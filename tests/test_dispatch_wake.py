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

import json
import sys
import textwrap
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchLimits,
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    ProjectDispatchSettings,
    RunnerMode,
)
from agentjobs.dispatch.runner import DispatchRunner, RunDirectory
from agentjobs.dispatch.wake import (
    WakeError,
    build_wake_prompt,
    find_wake_target,
    session_uuids,
    wake_argv,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger, Lifecycle
from agentjobs.storage import TaskStorage

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
    return TaskManager(TaskStorage(workspace / "tasks"))


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
    workspace: Path, manager: TaskManager, cli: Path, *, resume_sessions: bool = True
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
) -> RunDirectory:
    meta: Dict[str, object] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": "sandbox",
        "mode": "session",
        "posture": "auto",
        "status": status,
        "session_id": session_id,
        "started_at": started_at,
        "dispatch_entry_id": 3,
    }
    if reaped:
        meta["reaped"] = True
    return RunDirectory.create(home, run_id, meta)


def ran_argv(workspace: Path) -> List[str]:
    loaded = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    return [str(element) for element in loaded]


def ran_stdin(workspace: Path) -> str:
    return (workspace / "stdin.txt").read_text(encoding="utf-8")


# ----- choosing what to resume ------------------------------------------------


class TestFindWakeTarget:
    def test_a_task_with_no_previous_session_has_nothing_to_wake(self, workspace: Path) -> None:
        assert find_wake_target(workspace / "home", "task-001", rows=[]) is None

    def test_a_finished_session_still_in_the_ledger_is_the_target(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        target = find_wake_target(home, "task-001", rows=[stopped_row("aaaa1111", "aaaa1111-u")])

        assert target is not None
        assert target.previous_run_id == "run_previous"
        assert target.session_uuid == "aaaa1111-u"

    def test_the_full_uuid_is_carried_not_the_short_id(self, workspace: Path) -> None:
        """``--resume`` takes the UUID. Handing it the short id is a resume that fails."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        target = find_wake_target(
            home, "task-001", rows=[stopped_row("aaaa1111", "aaaa1111-2222-3333-4444-555555555555")]
        )

        assert target is not None
        assert target.session_uuid == "aaaa1111-2222-3333-4444-555555555555"
        assert target.session_uuid != target.session_id

    def test_a_live_run_is_not_woken(self, workspace: Path) -> None:
        """Something is already working it; whether to dispatch at all is the lock's call."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", status="running")

        assert find_wake_target(home, "task-001", rows=[stopped_row("aaaa1111", "u")]) is None

    def test_a_reaped_run_is_not_woken(self, workspace: Path) -> None:
        """``claude rm`` deleted the conversation; the run's own meta says so."""
        home = workspace / "home"
        seed_finished_run(home, "task-001", reaped=True)

        assert find_wake_target(home, "task-001", rows=[stopped_row("aaaa1111", "u")]) is None

    def test_a_session_the_manager_no_longer_lists_is_not_woken(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-001", session_id="aaaa1111")

        assert find_wake_target(home, "task-001", rows=[stopped_row("bbbb2222", "u")]) is None

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

        target = find_wake_target(home, "task-001", rows=[stopped_row("old00001", "old-uuid")])

        assert target is None

    def test_another_tasks_session_is_never_borrowed(self, workspace: Path) -> None:
        home = workspace / "home"
        seed_finished_run(home, "task-999", session_id="aaaa1111")

        assert find_wake_target(home, "task-001", rows=[stopped_row("aaaa1111", "u")]) is None


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
