"""Tests for the dispatch runner, in both modes.

The batch tests drive **real processes**, not mocks: a fake runner that exits non-zero,
one that hangs past the timeout, one that emits megabytes, and one that spawns a
grandchild and outlives it. A mocked ``Popen`` would pass while the thing that matters --
that a terminal entry exists no matter how the process ends, and that nothing is left
running -- silently did not work. Windows is the reference platform here.

The session tests drive a fake CLI that answers ``agents --json``, ``logs`` and ``stop``
the way Claude Code 2.1.228 was observed to (task-077 log entry 5). That is a deliberate
seam: session mode is defined operationally as "a runner whose executable answers
``agents --json``", so a fake that answers it exercises the real code path. The one thing
it cannot prove is that Claude Code still behaves that way, which is what the
undocumented-surface test at the bottom is for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchLimits,
    DispatchResolution,
    DispatchRunner as RunnerConfig,
    Posture,
    ProjectDispatchSettings,
    RunnerCandidate,
    RunnerMode,
    RunnerSelection,
    SelectionSource,
    SkipReason,
)
from agentjobs.dispatch.runner import (
    GUIDE_PATH,
    REMOTE_CONTROL_URL,
    TRANSCRIPT_FILENAME,
    DispatchRunner,
    DispatchRunError,
    SessionPhase,
    allow_list_settings,
    allow_rules,
    classify_session,
    compose_argv,
    drop_repainted_lines,
    posture_flags,
    readable_tail,
    resolve_executable,
    strip_ansi,
    uncommitted_paths,
    working_tree_clean,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, DispatchOutcome, Lifecycle, LogEntryType
from agentjobs.storage import TaskStorage

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----- fixtures ---------------------------------------------------------------


def write_script(path: Path, source: str) -> Path:
    """Write a Python script and return it."""
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def make_resolution(
    argv: List[str],
    *,
    mode: RunnerMode = RunnerMode.BATCH,
    posture: Posture = Posture.SUPERVISED,
    timeout: int = 1800,
    stale: int = 3600,
    require_clean_tree: bool = False,
) -> DispatchResolution:
    """A resolution as task-068's config layer would produce it."""
    runner = RunnerConfig(name="fake", argv=argv, env={}, mode=mode)
    settings = ProjectDispatchSettings(
        project_id="sandbox",
        enabled=True,
        runner="fake",
        require_clean_tree=require_clean_tree,
        posture=posture,
    )
    limits = DispatchLimits(run_timeout_seconds=timeout, session_stale_seconds=stale)
    return DispatchResolution(
        project_id="sandbox",
        runner=runner,
        settings=settings,
        limits=limits,
        config=DispatchConfig(enabled=True, limits=limits),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A project root and an AgentJobs home, both throwaway."""
    (tmp_path / "project").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "tasks").mkdir()
    return tmp_path


@pytest.fixture
def manager(workspace: Path) -> TaskManager:
    return TaskManager(TaskStorage(workspace / "tasks"))


@pytest.fixture
def task(manager: TaskManager):
    """A task in the state a dispatcher actually finds one in: ready, then claimed."""
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return manager.claim_task(created.id, agent="claude")


def build(workspace: Path, manager: TaskManager, resolution: DispatchResolution) -> DispatchRunner:
    return DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=workspace / "project",
        home=workspace / "home",
        api_base="http://localhost:8899",
        grace_seconds=2.0,
    )


def terminal_entries(manager: TaskManager, task_id: str) -> list:
    task = manager.get_task(task_id)
    assert task is not None
    return [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]


def join(runner_handle, timeout: float = 60.0) -> None:
    """Wait for a batch supervisor to finish."""
    assert runner_handle.supervisor is not None
    runner_handle.supervisor.join(timeout=timeout)
    assert not runner_handle.supervisor.is_alive(), "supervisor thread never finished"


# ----- the permission posture -------------------------------------------------


class TestPosture:
    def test_every_allow_rule_uses_the_colon_form(self) -> None:
        """A rule without the colon matches nothing and looks exactly like it working."""
        rules = allow_rules()

        assert rules, "the seed allow-list must not be empty"
        for rule in rules:
            assert rule.endswith(":*)"), rule
            tool, _, rest = rule.partition("(")
            assert tool in {"Bash", "PowerShell"}, rule
            assert ":" in rest, rule

    def test_the_seed_list_covers_the_boring_commands(self) -> None:
        rules = " ".join(allow_rules())

        for prefix in ("poetry run pytest", "git commit", "npm run"):
            assert f"({prefix}:*)" in rules

    def test_read_only_gets_no_tools_and_no_worktree(self) -> None:
        flags = posture_flags(Posture.READ_ONLY)

        assert flags == ["--tools", "Read,Glob,Grep,WebFetch"]
        assert "-w" not in flags

    def test_supervised_gets_accept_edits_and_the_allow_list(self) -> None:
        flags = posture_flags(Posture.SUPERVISED)

        assert flags[:2] == ["--permission-mode", "acceptEdits"]
        settings = json.loads(flags[flags.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules()

    def test_autonomous_gets_bypass_and_never_the_allow_list(self) -> None:
        """An allow-list under bypassPermissions would imply a limit that is not there."""
        flags = posture_flags(Posture.AUTONOMOUS)

        assert flags[:2] == ["--permission-mode", "bypassPermissions"]
        assert "--settings" not in flags

    def test_auto_gets_the_auto_mode_and_the_allow_list(self) -> None:
        """The default posture, per task-020.

        The mode string matters more than it looks: ``auto`` is the one mode that gates
        every action without needing a terminal to answer with. Getting ``acceptEdits``
        here instead would park the run on its first unlisted command, which is the
        defect this posture exists to fix, and nothing in a passing suite would say so.
        """
        flags = posture_flags(Posture.AUTO)

        assert flags[:2] == ["--permission-mode", "auto"]
        settings = json.loads(flags[flags.index("--settings") + 1])
        assert settings["permissions"]["allow"] == allow_rules()

    @pytest.mark.parametrize("posture", list(Posture))
    def test_no_posture_asks_the_cli_for_a_worktree(self, posture: Posture) -> None:
        """task-186. This assertion is the whole fix, so it is stated per posture.

        Every writing posture passed ``-w <task_id>`` until 2026-08-19. A session
        isolated that way refuses every git operation aimed at the shared checkout --
        by ``-C`` and by ``cd`` alike -- and the shared checkout is where task records
        are committed and where the merge gate runs. So a dispatched run could do the
        work and then neither record nor merge it, which is a defect no unit test on the
        flags would have caught, because the flags were exactly what was asked for.

        ``read_only`` is in the parametrisation deliberately: it never had a worktree,
        and the property that it still has none must not depend on the writing postures
        happening to be tested nearby.
        """
        assert "-w" not in posture_flags(posture)
        assert "--worktree" not in posture_flags(posture)

    def test_the_composed_argv_of_every_posture_is_exactly_this(
        self, workspace: Path, manager: TaskManager
    ) -> None:
        """The whole command, per posture, not just the flags in isolation.

        A flags-only assertion cannot see where the flags land, whether the prompt
        survived, or whether something else in the pipeline reintroduced ``-w``. This
        one names the full argv, so any of those shows up as a diff rather than as a
        run that behaves oddly in production.
        """
        expected = {
            Posture.READ_ONLY: ["--tools", "Read,Glob,Grep,WebFetch"],
            Posture.AUTO: ["--permission-mode", "auto", "--settings", allow_list_settings()],
            Posture.SUPERVISED: [
                "--permission-mode",
                "acceptEdits",
                "--settings",
                allow_list_settings(),
            ],
            Posture.AUTONOMOUS: ["--permission-mode", "bypassPermissions"],
        }
        assert set(expected) == set(Posture), "a new posture needs its argv named here"

        for posture, flags in expected.items():
            runner = build(
                workspace,
                manager,
                make_resolution(
                    ["claude", "--bg", "--remote-control", "{prompt}"], posture=posture
                ),
            )

            argv = runner.build_argv("task-070-example", "run_abcd1234")

            assert argv[:3] == [resolve_executable("claude"), "--bg", "--remote-control"]
            assert argv[3:-1] == flags, posture
            assert argv[-1] == runner.build_prompt("task-070-example", "run_abcd1234")

    def test_the_config_and_schema_posture_enums_stay_in_step(self) -> None:
        """Two enums spell the same concept, and a run needs both.

        ``runner._record_dispatch`` converts the config posture into the schema one by
        value, so a posture present in only one of them raises at dispatch time rather
        than at import. Adding ``auto`` to the config enum alone did exactly that.
        """
        from agentjobs.models_v2 import DispatchPosture

        assert {posture.value for posture in Posture} == {
            posture.value for posture in DispatchPosture
        }

    def test_each_posture_composes_a_distinct_permission_mode(self) -> None:
        """A posture that silently collapsed onto another's mode would look fine."""
        modes = {
            posture: posture_flags(posture)[1]
            for posture in (Posture.AUTO, Posture.SUPERVISED, Posture.AUTONOMOUS)
        }

        assert modes == {
            Posture.AUTO: "auto",
            Posture.SUPERVISED: "acceptEdits",
            Posture.AUTONOMOUS: "bypassPermissions",
        }


class TestArgvComposition:
    def test_posture_flags_land_before_the_prompt(self) -> None:
        argv = compose_argv(
            ["claude", "--bg", "{prompt}"],
            {"prompt": "read the record"},
            ["--permission-mode", "acceptEdits"],
        )

        assert argv == ["claude", "--bg", "--permission-mode", "acceptEdits", "read the record"]

    def test_flags_are_appended_when_there_is_no_prompt_element(self) -> None:
        argv = compose_argv(["claude", "agents"], {"prompt": "unused"}, ["--json"])

        assert argv == ["claude", "agents", "--json"]

    def test_a_hostile_prompt_is_still_exactly_one_element(self) -> None:
        prompt = 'go; rm -rf / && echo "x" `id`\nsecond line'

        argv = compose_argv(["claude", "-p", "{prompt}"], {"prompt": prompt}, [])

        assert argv == ["claude", "-p", prompt]


class TestPromptStub:
    def test_the_guide_it_points_at_exists_and_links_to_the_contract(self) -> None:
        """The stub once named a v1-era file, so every run started at a stale document."""
        guide = REPO_ROOT / GUIDE_PATH

        assert guide.is_file(), f"{GUIDE_PATH} does not exist"
        text = guide.read_text(encoding="utf-8")
        assert "resumption contract" in text.lower()
        assert "schema-design.md" in text

    def test_the_stub_is_a_pointer_not_a_composition(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert task.id in prompt
        assert GUIDE_PATH in prompt
        assert "run_abcd1234" in prompt
        # It must not restate the record, which is the whole argument for a stub.
        assert task.spec.description not in prompt
        assert len(prompt) < 500

    def test_the_stub_tells_the_run_to_take_its_own_worktree(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """task-186. The one instruction that cannot be deferred to the guide.

        Dispatch stopped passing ``-w``, so nothing isolates a dispatched run from the
        project's shared working tree but its own first act. Every other thing the run
        needs to know it can go and read; this it has to know *before* it reads
        anything. Asserted on the rendered prompt rather than on ``PROMPT_STUB``,
        because what reaches the agent is what matters.
        """
        runner = build(workspace, manager, make_resolution(["fake"]))

        prompt = runner.build_prompt(task.id, "run_abcd1234")

        assert "worktree" in prompt
        assert "not isolated" in prompt.lower()

    def test_the_guide_states_the_worktree_requirement_too(self) -> None:
        """The stub is one clause; the guide is where the reasoning lives.

        Both, deliberately -- and this test exists so that deleting either half is a
        failure rather than a quiet drift back to a prompt that assumes containment
        somebody else arranged.
        """
        text = (REPO_ROOT / GUIDE_PATH).read_text(encoding="utf-8")

        assert "git worktree add" in text
        heading = "## Before you write anything: take your own worktree"
        assert heading in text
        # Unmissable means near the top, not beside the claim halfway down.
        assert text.index(heading) < len(text) // 3


# ----- batch mode -------------------------------------------------------------


class TestBatchOutcomes:
    def test_a_clean_exit_that_moved_the_ball_is_completed(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(tmp_path / "ok.py", "print('done')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        # The agent would move the ball itself; do it for the fake one.
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.COMPLETED.value
        assert results[0].data["exit_code"] == 0

    def test_a_clean_exit_that_never_moved_the_ball_is_a_failure(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """Exit 0 with an unmoved ball means the agent stopped without saying what it needs."""
        script = write_script(tmp_path / "quiet.py", "print('nothing to say')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FINISHED_WITHOUT_HANDOFF.value
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN

    def test_a_non_zero_exit_is_failed_and_inlines_the_output(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(
            tmp_path / "boom.py",
            """
            import sys
            print("about to fail")
            print("stack-ish detail", file=sys.stderr)
            sys.exit(3)
            """,
        )
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FAILED.value
        assert results[0].data["exit_code"] == 3
        assert "about to fail" in (results[0].body or "")

    def test_a_hang_past_the_timeout_is_terminated_and_reported(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        script = write_script(
            tmp_path / "hang.py",
            """
            import time
            print("sleeping", flush=True)
            time.sleep(600)
            """,
        )
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(script), "{prompt}"], timeout=2),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.TIMEOUT.value

    def test_megabytes_of_output_go_to_disk_and_do_not_stall_the_run(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """Buffering in memory loses exactly the case that matters: a crash mid-flood."""
        script = write_script(
            tmp_path / "flood.py",
            """
            for index in range(20000):
                print("x" * 100)
            """,
        )
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        stdout = handle.directory.path / "stdout.log"
        assert stdout.stat().st_size > 1_000_000
        assert len(terminal_entries(manager, task.id)) == 1

    def test_an_exception_in_the_supervisor_itself_still_writes_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path, monkeypatch
    ) -> None:
        """The task-047 shape: a supervisor that dies silently is worse than none."""
        script = write_script(tmp_path / "ok.py", "print('done')\n")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected supervisor failure")

        monkeypatch.setattr(DispatchRunner, "_classify_batch_exit", explode)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        join(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.CRASHED.value
        assert "injected supervisor failure" in (results[0].body or "")

    def test_a_runner_that_cannot_be_spawned_still_gets_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        runner = build(workspace, manager, make_resolution(["definitely-not-a-real-binary"]))

        with pytest.raises(DispatchRunError):
            runner.start(task, actor="Jeff Posey", caused_by=1)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.CRASHED.value


class TestProcessGroup:
    def test_the_timeout_kills_the_grandchild_too(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """An agent that shelled out to pytest must not leave the pytest behind."""
        marker = tmp_path / "grandchild.pid"
        grandchild = write_script(
            tmp_path / "grandchild.py",
            f"""
            import os, time
            open(r"{marker}", "w").write(str(os.getpid()))
            time.sleep(600)
            """,
        )
        parent = write_script(
            tmp_path / "parent.py",
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, r"{grandchild}"])
            time.sleep(600)
            """,
        )
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(parent), "{prompt}"], timeout=3),
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        deadline = 30
        while not marker.exists() and deadline:
            deadline -= 1
            subprocess.run([sys.executable, "-c", "import time; time.sleep(0.2)"], check=False)
        assert marker.exists(), "the grandchild never started, so this proves nothing"
        grandchild_pid = int(marker.read_text())

        join(handle)

        assert not _pid_alive(grandchild_pid), f"pid {grandchild_pid} survived the timeout"


def _pid_alive(pid: int) -> bool:
    """True when a pid is still running. Windows is the reference platform."""
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ----- session mode -----------------------------------------------------------


FAKE_CLI = """
import json, sys, pathlib

# A real CLI writes UTF-8; without this the box-drawing below raises inside this
# process on a cp1252 console and the transcript silently truncates mid-frame.
sys.stdout.reconfigure(encoding="utf-8")

state_file = pathlib.Path(__file__).with_name("ledger.json")
argv = sys.argv[1:]

if argv and argv[0] == "agents":
    print(json.dumps(json.loads(state_file.read_text())))
    raise SystemExit(0)

if argv and argv[0] == "logs":
    # Shaped like the real thing: escape sequences, box-drawing frame, and the
    # Remote Control URL that appears only here and never in the ledger.
    print("[38;2;153;153;153m/remote-control is active · Continue here, on your phone, or at")
    print("https://claude.ai/code/session_0142VngQLPx14GUrbyfLWPuC[m")
    print("╭" + "─" * 40 + "╮")
    print("[1mClaude needs your permission to run:[m")
    print("  poetry run alembic upgrade head")
    print("╰" + "─" * 40 + "╯")
    raise SystemExit(0)

if argv and argv[0] == "stop":
    rows = json.loads(state_file.read_text())
    state_file.write_text(json.dumps([r for r in rows if r["id"] != argv[1]]))
    print("stopped")
    raise SystemExit(0)

# Launch: behave like `--bg`, which prints a short id and returns immediately.
state_file.write_text(json.dumps([{
    "id": "b55b35ad", "sessionId": "session_0142Vng", "pid": 4242,
    "kind": "background", "status": "busy", "state": "working",
}]))
print("backgrounded \\u00b7 b55b35ad \\u00b7 aj-task")
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    return write_script(tmp_path / "fakecli.py", FAKE_CLI)


def session_resolution(fake_cli: Path, **kwargs: object) -> DispatchResolution:
    return make_resolution(
        [sys.executable, str(fake_cli), "--bg", "--remote-control", "{prompt}"],
        mode=RunnerMode.SESSION,
        **kwargs,  # type: ignore[arg-type]
    )


def set_ledger(fake_cli: Path, rows: List[dict]) -> None:
    (fake_cli.parent / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")


class TestSessionClassification:
    @pytest.mark.parametrize(
        "status,state,expected",
        [
            ("busy", "working", SessionPhase.RUNNING),
            ("waiting", "blocked", SessionPhase.PARKED),
            ("idle", "done", SessionPhase.FINISHED),
            ("idle", "blocked", SessionPhase.FINISHED),
            (None, "stopped", SessionPhase.STOPPED),
        ],
    )
    def test_the_observed_pairs_map_as_verified(
        self, status: Optional[str], state: Optional[str], expected: SessionPhase
    ) -> None:
        assert classify_session(status, state) is expected

    def test_an_unrecognised_pair_is_treated_as_still_running(self) -> None:
        """Declaring a live run over would write a terminal entry for a working session."""
        assert classify_session("something-new", "unheard-of") is SessionPhase.RUNNING

    def test_idle_blocked_is_finished_not_parked(self) -> None:
        """It finished after a denial; reading it as parked asks a human a dead question."""
        assert classify_session("idle", "blocked") is SessionPhase.FINISHED


class TestSessionMode:
    def test_it_captures_the_id_the_cli_assigned(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """--bg ignores --session-id and manages the id itself, so we read it back."""
        runner = build(workspace, manager, session_resolution(fake_cli))

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert handle.session_id == "b55b35ad"
        assert handle.run_id != handle.session_id
        after = manager.get_task(task.id)
        assert after is not None
        dispatched = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        assert dispatched.data["session_id"] == "b55b35ad"
        assert dispatched.data["run_id"] == handle.run_id
        assert dispatched.data["mode"] == "session"

    def test_no_session_id_argument_is_ever_passed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))

        argv = runner.build_argv(task.id, "run_abcd1234")

        assert "--session-id" not in argv

    def test_a_launcher_that_prints_no_id_is_a_hard_failure(
        self, workspace: Path, manager: TaskManager, task, tmp_path: Path
    ) -> None:
        """A session nothing can follow is worse than one that never started."""
        silent = write_script(tmp_path / "silent.py", "print('started, good luck')\n")
        runner = build(
            workspace,
            manager,
            make_resolution([sys.executable, str(silent), "{prompt}"], mode=RunnerMode.SESSION),
        )

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "could not read its id" in str(caught.value)

    def test_a_parked_session_becomes_a_question_a_human_can_answer(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Without this, the supervised posture is not a safety property, it is a hang."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(
            fake_cli,
            [{"id": "b55b35ad", "status": "waiting", "state": "blocked", "pid": 4242}],
        )

        phase = runner.poll_session(handle)

        assert phase is SessionPhase.PARKED
        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.HUMAN
        assert after.ball_reason is BallReason.INPUT
        prompt = after.ball_prompt or ""
        assert "poetry run alembic upgrade head" in prompt
        assert "b55b35ad" in prompt
        # Leads with the thing a human can actually act on from a phone.
        assert "https://claude.ai/code/session_0142VngQLPx14GUrbyfLWPuC" in prompt
        # And is readable: no escape sequences, no frame-only lines.
        assert "" not in prompt
        assert "[38;2;" not in prompt
        assert "───" not in prompt

    def test_a_parked_session_is_never_escalated_or_killed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """A timeout is not a human act, so it cannot grant autonomy (design section 2)."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=0))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "waiting", "state": "blocked"}])

        runner.poll_session(handle)
        runner.poll_session(handle)

        assert terminal_entries(manager, task.id) == []
        rows = json.loads((fake_cli.parent / "ledger.json").read_text())
        assert rows, "the parked session was stopped, which the design forbids"

    def test_a_finished_session_that_moved_the_ball_is_completed_and_reaped(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please review.",
        )
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.COMPLETED.value
        assert json.loads((fake_cli.parent / "ledger.json").read_text()) == [], "not reaped"

    def test_a_finished_session_inside_the_staleness_window_is_left_alone(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An agent pausing mid-work looks identical to one that stopped for good."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=3600))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        assert terminal_entries(manager, task.id) == []

    def test_a_stale_session_is_reported_but_not_killed(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Staleness replaces the wall-clock kill precisely so the session stays attachable."""
        runner = build(workspace, manager, session_resolution(fake_cli, stale=0))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.FINISHED_WITHOUT_HANDOFF.value
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN
        rows = json.loads((fake_cli.parent / "ledger.json").read_text())
        assert rows, "a stale session must stay attachable, not be stopped"

    def test_a_session_that_vanished_from_the_ledger_gets_a_terminal_entry(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        set_ledger(fake_cli, [])

        phase = runner.poll_session(handle)

        assert phase is SessionPhase.GONE
        results = terminal_entries(manager, task.id)
        assert len(results) == 1
        assert results[0].data["outcome"] == DispatchOutcome.INTERRUPTED.value

    def test_the_ledger_is_scoped_to_this_project(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An unrelated session elsewhere must never be mistaken for a dispatched run."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        runner.start(task, actor="Jeff Posey", caused_by=1)

        recorded = (workspace / "home" / "runs").iterdir()
        assert any(recorded), "no run directory was written"
        # The scoping itself: --cwd is always passed.
        argv_seen: List[str] = []
        original = subprocess.run

        def capture(argv, *args, **kwargs):
            argv_seen.extend(argv)
            return original(argv, *args, **kwargs)

        import agentjobs.dispatch.runner as runner_module

        runner_module.subprocess.run = capture  # type: ignore[assignment]
        try:
            runner.ledger()
        finally:
            runner_module.subprocess.run = original

        assert "--cwd" in argv_seen
        assert str(workspace / "project") in argv_seen


class TestTranscriptCapture:
    """The run directory has to hold the session's output, because nothing else will.

    ``stdout.log`` for a session run is the launcher's backgrounding banner and can never
    be anything else, and the session's own transcript lives in a store AgentJobs does
    not own and does not outlive the reap.
    """

    def test_a_capture_writes_the_transcript_beside_the_run_metadata(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        runner.capture_transcript(handle)

        written = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "poetry run alembic upgrade head" in written
        # Raw, escape sequences and all. Stripping happens where it is rendered, so the
        # stored copy stays the thing the terminal actually showed.
        assert "\x1b[" in written

    def test_an_unreadable_transcript_does_not_erase_the_last_good_one(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """ "Could not read it just now" is not evidence the session produced nothing."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        runner.capture_transcript(handle)
        original = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")

        runner.transcript = lambda session_id: ""  # type: ignore[method-assign]
        runner.capture_transcript(handle)

        assert (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8") == original

    def test_polling_captures_before_it_settles_and_reaps(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """Ordering is the whole point: `claude logs` on a reaped session reads nothing,
        so capturing after settling would leave every completed run blank."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Done, please look.",
        )
        set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        runner.poll_session(handle)

        assert json.loads((fake_cli.parent / "ledger.json").read_text()) == [], "not reaped"
        kept = (handle.directory.path / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "poetry run alembic upgrade head" in kept


class TestTranscriptRendering:
    def test_escape_sequences_are_removed(self) -> None:
        """A ball prompt full of CSI sequences is unusable where it must be answered."""
        assert strip_ansi("[1mbold[m plain") == "bold plain"

    def test_frame_only_lines_are_dropped(self) -> None:
        raw = "\n".join(["╭" + "─" * 10 + "╮", "real content", "╰" + "─" * 10 + "╯"])

        assert readable_tail(raw, 40) == "real content"

    def test_the_tail_keeps_the_end_not_the_beginning(self) -> None:
        raw = "\n".join(f"line {index}" for index in range(100))

        assert readable_tail(raw, 3) == "line 97\nline 98\nline 99"

    def test_the_remote_control_url_is_found_through_the_escape_codes(self) -> None:
        raw = "[38;2;1;2;3mopen https://claude.ai/code/session_ABC123[m now"

        match = REMOTE_CONTROL_URL.search(strip_ansi(raw))

        assert match is not None
        assert match.group(0) == "https://claude.ai/code/session_ABC123"

    def test_a_transcript_without_a_url_is_not_an_error(self) -> None:
        assert REMOTE_CONTROL_URL.search("nothing here") is None


class TestExecutableResolution:
    def test_a_windows_shim_is_resolved_rather_than_shelled_out_to(self) -> None:
        """`claude` is a .CMD on Windows; Popen without a shell cannot find it by name."""
        resolved = resolve_executable("python")

        assert resolved != "python" or os.name != "nt"
        assert Path(resolved).exists() or resolved == "python"

    def test_an_unresolvable_name_is_returned_unchanged(self) -> None:
        """So the failure is subprocess's, naming the program, not a silent substitution."""
        assert resolve_executable("definitely-not-installed-xyz") == "definitely-not-installed-xyz"

    def test_the_prompt_a_human_is_told_to_type_is_not_the_resolved_path(
        self, workspace: Path, manager: TaskManager, fake_cli: Path
    ) -> None:
        runner = build(workspace, manager, session_resolution(fake_cli))

        assert runner.display_command() == sys.executable
        assert runner.build_argv("task-1", "run_1")[0] == resolve_executable(sys.executable)


class TestSpawnPreconditions:
    def test_the_sentinel_refuses_a_spawn_even_after_resolution_succeeded(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """The panic button must stop the next run, not the next config reload."""
        runner = build(workspace, manager, session_resolution(fake_cli))
        (workspace / "home" / "DISPATCH_DISABLED").write_text("", encoding="utf-8")

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "DISPATCH_DISABLED" in str(caught.value)
        assert terminal_entries(manager, task.id) == []

    def test_a_dirty_tree_refuses_the_run(
        self, workspace: Path, manager: TaskManager, task, fake_cli: Path
    ) -> None:
        """An agent committing on top of uncommitted human work entangles the two."""
        project = workspace / "project"
        subprocess.run(["git", "init"], cwd=project, capture_output=True, check=True)
        (project / "in-flight.txt").write_text("someone is mid-edit", encoding="utf-8")
        runner = build(workspace, manager, session_resolution(fake_cli, require_clean_tree=True))

        with pytest.raises(DispatchRunError) as caught:
            runner.start(task, actor="Jeff Posey", caused_by=1)
        assert "uncommitted" in str(caught.value)


class TestUncommittedPaths:
    """The primitive both clean-tree gates ask, including what it deliberately ignores."""

    @staticmethod
    def repo(root: Path) -> Path:
        """A committed repository with a tasks directory tracked inside it."""
        (root / "tasks").mkdir(parents=True)
        (root / "src").mkdir()
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
        (root / "tasks" / "task-001.yaml").write_text("id: task-001\n", encoding="utf-8")
        (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
        return root

    def test_a_clean_tree_has_no_paths(self, tmp_path: Path) -> None:
        root = self.repo(tmp_path / "proj")
        assert uncommitted_paths(root) == []
        assert working_tree_clean(root)

    def test_git_that_cannot_answer_is_not_clean(self, tmp_path: Path) -> None:
        """Distinct from "nothing is uncommitted", and it has to refuse rather than pass."""
        nowhere = tmp_path / "not-a-repo"
        nowhere.mkdir()
        assert uncommitted_paths(nowhere) is None
        assert working_tree_clean(nowhere) is False

    def test_the_ignored_directory_drops_out_and_nothing_else_does(self, tmp_path: Path) -> None:
        root = self.repo(tmp_path / "proj")
        (root / "tasks" / "task-001.yaml").write_text(
            "id: task-001\nclaimed: y\n", encoding="utf-8"
        )
        (root / "tasks" / "task-002.yaml").write_text("id: task-002\n", encoding="utf-8")
        (root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        assert sorted(uncommitted_paths(root) or []) == [
            "src/app.py",
            "tasks/task-001.yaml",
            "tasks/task-002.yaml",
        ]
        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/app.py"]

    def test_a_filename_with_a_space_survives_the_parse(self, tmp_path: Path) -> None:
        """`git status --porcelain` quotes and escapes these; the -z form does not.

        A path this misparsed would be dropped silently from a safety check, so it is
        pinned rather than left to the format.
        """
        root = self.repo(tmp_path / "proj")
        (root / "src" / "two words.py").write_text("x = 3\n", encoding="utf-8")

        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/two words.py"]

    def test_a_rename_reports_the_new_path_once(self, tmp_path: Path) -> None:
        """-z emits the original path as a second field, which must not be read as dirt."""
        root = self.repo(tmp_path / "proj")
        subprocess.run(
            ["git", "-C", str(root), "mv", "src/app.py", "src/renamed.py"],
            capture_output=True,
            check=True,
        )

        assert uncommitted_paths(root, ignore=[root / "tasks"]) == ["src/renamed.py"]

    def test_paths_resolve_against_the_repository_root_not_the_directory_asked(
        self, tmp_path: Path
    ) -> None:
        """Porcelain paths are repo-relative wherever git ran, so the exclusion must be too."""
        root = self.repo(tmp_path / "proj")
        (root / "tasks" / "task-001.yaml").write_text(
            "id: task-001\nclaimed: y\n", encoding="utf-8"
        )
        (root / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        assert uncommitted_paths(root / "src", ignore=[root / "tasks"]) == ["src/app.py"]


# ----- the shapes this module refuses to have ---------------------------------


class TestShapesRefused:
    def test_there_is_no_asyncio_and_no_shell(self) -> None:
        """task-047's detached-coroutine shape, and shell=True, are both out by rule.

        Read from the syntax tree rather than by string search, so the prose explaining
        why these are forbidden cannot fail the test that forbids them.
        """
        import ast

        tree = ast.parse(
            (REPO_ROOT / "src" / "agentjobs" / "dispatch" / "runner.py").read_text(encoding="utf-8")
        )

        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "asyncio" not in imported

        shell_kwargs = [
            keyword
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "shell"
        ]
        assert shell_kwargs == []

    def test_the_transcript_is_never_parsed_for_state(self) -> None:
        """Structured state comes from the ledger; `logs` is an ANSI pty scrape."""
        source = (REPO_ROOT / "src" / "agentjobs" / "dispatch" / "runner.py").read_text(
            encoding="utf-8"
        )
        body = source.split("def transcript(", 1)[1].split("\n    def ", 1)[0]

        # All that is done with `logs` output is hand it back for a human to read.
        assert "json.loads" not in body
        assert "classify" not in body


class TestRepaintCollapsing:
    """A TUI repaints its whole screen, so its pty capture holds the same frame many
    times. Forty lines of a real session were thirteen distinct lines painted three
    times over, with the newest work pushed off the end by copies of itself."""

    def test_a_repainted_screen_is_shown_once(self) -> None:
        frame = "> reading the task record\n  running tests\n"

        collapsed = drop_repainted_lines(frame * 3)

        assert collapsed.splitlines() == ["> reading the task record", "  running tests"]

    def test_the_newest_copy_is_the_one_kept(self) -> None:
        """Order has to follow the newest frame; keeping the first copy would show the
        opening screen and drop everything that happened after it."""
        collapsed = drop_repainted_lines("opened\nstep one\nopened\nstep one\nstep two\n")

        assert collapsed.splitlines() == ["opened", "step one", "step two"]

    def test_nothing_is_lost_when_a_transcript_never_repeats(self) -> None:
        text = "one\ntwo\nthree"

        assert drop_repainted_lines(text) == text


# ----- the group audit trail (task-177) ---------------------------------------


def grouped_resolution(argv: List[str]) -> DispatchResolution:
    """A resolution as the group selector produces one: a winner plus its rivals."""
    winner = RunnerConfig(name="second", argv=argv, env={}, mode=RunnerMode.BATCH)
    settings = ProjectDispatchSettings(
        project_id="sandbox", enabled=True, group="default", require_clean_tree=False
    )
    limits = DispatchLimits()
    selection = RunnerSelection(
        runner=winner,
        source=SelectionSource.DISPATCH,
        group="default",
        candidates=[
            RunnerCandidate(
                runner="first",
                eligible=False,
                skipped_because=SkipReason.DISABLED,
                detail="no key on this machine yet",
            ),
            RunnerCandidate(runner="second", eligible=True),
            RunnerCandidate(runner="third", eligible=True),
        ],
    )
    return DispatchResolution(
        project_id="sandbox",
        runner=winner,
        settings=settings,
        limits=limits,
        config=DispatchConfig(enabled=True, limits=limits),
        selection=selection,
    )


class TestSelectionIsRecorded:
    """sc-3: the dispatch entry answers "why that runner" without the local run dir."""

    def test_the_entry_names_the_group_the_winner_and_the_rivals(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        script = write_script(workspace / "ok.py", "print('done')")
        runner = build(
            workspace, manager, grouped_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        after = manager.get_task(task.id)
        assert after is not None
        entry = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        selection = entry.data["selection"]
        assert entry.data["runner"] == "second"
        assert selection["group"] == "default"
        assert selection["source"] == "dispatch"
        assert [c["runner"] for c in selection["candidates"]] == ["first", "second", "third"]
        skipped = selection["candidates"][0]
        assert skipped["eligible"] is False
        assert skipped["skipped_because"] == "disabled"
        assert skipped["detail"] == "no key on this machine yet"

    def test_the_handle_says_what_was_chosen_and_from_where(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        script = write_script(workspace / "ok2.py", "print('done')")
        runner = build(
            workspace, manager, grouped_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        assert handle.runner == "second"
        assert handle.group == "default"

    def test_a_flat_resolution_writes_no_selection_key_at_all(
        self, workspace: Path, manager: TaskManager, task
    ) -> None:
        """The compatibility claim, asserted on the bytes rather than on intent."""
        script = write_script(workspace / "ok3.py", "print('done')")
        runner = build(
            workspace, manager, make_resolution([sys.executable, str(script), "{prompt}"])
        )

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        if handle.supervisor:
            handle.supervisor.join(timeout=30)

        after = manager.get_task(task.id)
        assert after is not None
        entry = [e for e in after.log if e.type is LogEntryType.DISPATCH][0]
        assert "selection" not in entry.data
        assert handle.group is None
