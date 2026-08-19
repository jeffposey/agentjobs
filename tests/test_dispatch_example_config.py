"""The shipped example config, **run** rather than only parsed.

``tests/test_dispatch_cli.py`` already proves ``agentjobs dispatch example`` prints
something this build can load. That is not enough, and task-177's review found out why:
the example declared its Claude runners ``mode: session`` while giving them ``-p``, the
batch flag, and no ``--remote-control``. Every parse-level check passed. The config every
new setup inherits could not have started a steerable session.

So this module drives the **unmodified** ``EXAMPLE_CONFIG`` through the real path a
dispatch takes -- ``assert_dispatch_permitted`` -> ``resolve_runner`` -> ``build_argv``
-> ``start`` -> ``poll_session`` -> the handoff a human reads -- against a shim standing
in for the CLI the example names. Only two things are edited into the file, and they are
the two edits a human makes: the master switch, and one project entry. The runners and
the groups are byte-identical to what ships, and one test asserts that.

**The shim is faithful in the ways that matter, and refuses to be faithful in the ways
that would hide a bug.** It prints a Remote Control URL in its transcript only when it
was actually launched with ``--remote-control``, and it prints a backgrounded id only
when it was launched with ``--bg`` and not ``-p``. So the assertion at the end of the
chain is on what a person would see -- "the ball prompt carries a link that works from a
phone" -- and the shipped defect fails it rather than passing quietly.

What a shim cannot prove is that Claude Code still behaves this way. That is what
``TestTheFlagsExistOnTheInstalledCli`` is for: where the named CLI is genuinely on PATH
it checks the example's flags against that CLI's own help, and where it is not, it skips.
CI never has the CLI; the machine writing the example always does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from agentjobs.dispatch.config import (
    RunnerMode,
    assert_dispatch_permitted,
    dispatch_config_path,
    load_dispatch_config,
)
from agentjobs.dispatch.runner import DispatchRunner, SessionPhase
from agentjobs.dispatch.scaffold import EXAMPLE_CONFIG, write_example_config
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, DispatchMode, Lifecycle, LogEntryType
from agentjobs.storage import TaskStorage

PROJECT_ID = "sandbox"

SESSION_GROUPS = ["standard", "deep", "quick"]
"""Every group in the example whose winning member declares ``mode: session``."""


# ----- a stand-in for the CLI the example names -------------------------------

SHIM = r'''
"""Enough of Claude Code's observable behaviour for a dispatch to run end to end.

Faithful where the dispatcher depends on it, and deliberately unforgiving where being
generous would let a broken example pass: no `backgrounded` line under `-p`, and no
Remote Control URL in the transcript unless `--remote-control` was on the launch.
"""
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

here = pathlib.Path(__file__).parent
argv = sys.argv[1:]

if argv and argv[0] == "agents":
    print(json.dumps(json.loads((here / "ledger.json").read_text(encoding="utf-8"))))
    raise SystemExit(0)

if argv and argv[0] == "logs":
    state = json.loads((here / "launch.json").read_text(encoding="utf-8"))
    print("\x1b[38;2;153;153;153m╭" + "─" * 40 + "╮")
    if state["remote_control"]:
        print("/remote-control is active · Continue here, on your phone, or at")
        print("https://claude.ai/code/session_0142VngQLPx14GUrbyfLWPuC\x1b[m")
    print("\x1b[1mClaude needs your permission to run:\x1b[m")
    print("  poetry run pytest tests/test_dispatch_example_config.py")
    print("╰" + "─" * 40 + "╯")
    raise SystemExit(0)

if argv and argv[0] == "stop":
    (here / "ledger.json").write_text("[]", encoding="utf-8")
    print("stopped")
    raise SystemExit(0)

# A launch. Record it verbatim: the test asserts on what the CLI was actually handed.
calls = json.loads((here / "calls.json").read_text(encoding="utf-8"))
calls.append(argv)
(here / "calls.json").write_text(json.dumps(calls), encoding="utf-8")

if "-p" in argv or "--print" in argv:
    # Batch: print and exit. No session id, because there is no session.
    print(json.dumps({"type": "result", "subtype": "success", "result": "OK"}))
    raise SystemExit(0)

if "--bg" not in argv and "--background" not in argv:
    # An interactive session with no TTY. A dispatch must never reach this.
    print("refusing to start an interactive session without a terminal", file=sys.stderr)
    raise SystemExit(1)

(here / "launch.json").write_text(
    json.dumps({"remote_control": "--remote-control" in argv}), encoding="utf-8"
)
(here / "ledger.json").write_text(
    json.dumps(
        [
            {
                "id": "b55b35ad",
                "sessionId": "session_0142Vng",
                "pid": 4242,
                "kind": "background",
                "status": "waiting",
                "state": "blocked",
            }
        ]
    ),
    encoding="utf-8",
)
print("backgrounded · b55b35ad · aj-sandbox")
'''


@pytest.fixture
def cli_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a program named ``claude`` on PATH and return its state directory.

    Named ``claude`` on purpose. The example's argv says ``claude``, and resolving that
    name through PATH -- including the ``.CMD`` shim npm installs on Windows -- is part
    of what is under test; a fake spawned as an absolute ``sys.executable`` never
    exercises it.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "shim.py"
    script.write_text(textwrap.dedent(SHIM), encoding="utf-8")
    (directory / "calls.json").write_text("[]", encoding="utf-8")
    (directory / "ledger.json").write_text("[]", encoding="utf-8")

    if os.name == "nt":
        launcher = directory / "claude.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "claude"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)

    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("claude"), "the shim did not land on PATH"
    return directory


def launches(shim: Path) -> List[List[str]]:
    """Every argv the shim was launched with, in order."""
    recorded: List[List[str]] = json.loads((shim / "calls.json").read_text(encoding="utf-8"))
    return recorded


# ----- the example, armed the way a human arms it -----------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An AgentJobs home holding the shipped example, with the two edits a human makes.

    Written through ``write_example_config`` rather than pasted, so the file under test
    is the one ``--write`` produces. The master switch and one project entry are then
    edited as text, which leaves the ``runners:`` and ``runner_groups:`` blocks -- the
    part this module is about -- byte-identical to what ships.
    """
    directory = tmp_path / "home"
    directory.mkdir()
    path = write_example_config(directory)
    text = path.read_text(encoding="utf-8")

    switch = "# `agentjobs dispatch status`, and turn this on when you mean it.\nenabled: false"
    assert switch in text, "the master switch is no longer where this fixture thinks it is"
    text = text.replace(switch, switch.replace("enabled: false", "enabled: true"))
    text = text.replace(
        "projects: {}",
        f"projects:\n  {PROJECT_ID}:\n    enabled: true\n    require_clean_tree: false\n",
    )
    path.write_text(text, encoding="utf-8")
    return directory


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def manager(tmp_path: Path) -> TaskManager:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    return TaskManager(TaskStorage(tasks))


@pytest.fixture
def task(manager: TaskManager):
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch from the shipped example.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return manager.claim_task(created.id, agent="claude")


def dispatcher(home: Path, project_root: Path, manager: TaskManager, group: str) -> DispatchRunner:
    """The dispatcher a real run gets, resolved through every gate for ``group``."""
    resolution = assert_dispatch_permitted(PROJECT_ID, home, group=group)
    return DispatchRunner(
        manager=manager,
        resolution=resolution,
        project_root=project_root,
        home=home,
        api_base="http://localhost:8899",
        grace_seconds=2.0,
    )


# ----- the example is exercised -----------------------------------------------


class TestEverySessionGroupStartsAFollowableSession:
    """The defect this module exists for: a session runner that cannot start a session."""

    @pytest.mark.parametrize("group", SESSION_GROUPS)
    def test_it_reaches_a_session_whose_id_the_dispatcher_captured(
        self,
        group: str,
        cli_shim: Path,
        home: Path,
        project_root: Path,
        manager: TaskManager,
        task,
    ) -> None:
        runner = dispatcher(home, project_root, manager, group)

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert handle.mode is DispatchMode.SESSION
        assert handle.session_id == "b55b35ad"
        assert handle.group == group
        launch = launches(cli_shim)[0]
        assert "--bg" in launch
        assert "-p" not in launch and "--print" not in launch

    @pytest.mark.parametrize("group", SESSION_GROUPS)
    def test_a_parked_run_hands_over_a_link_that_works_from_a_phone(
        self,
        group: str,
        cli_shim: Path,
        home: Path,
        project_root: Path,
        manager: TaskManager,
        task,
    ) -> None:
        """The capability ``--remote-control`` buys, asserted where a person would see it.

        Not "``--remote-control`` is in the argv" -- that is the same class of check the
        shipped defect passed. The shim withholds the URL when the flag is absent, so
        this fails on the argv that lost the feature, and it fails with the symptom.
        """
        runner = dispatcher(home, project_root, manager, group)
        handle = runner.start(task, actor="Jeff Posey", caused_by=1)

        assert runner.poll_session(handle) is SessionPhase.PARKED

        after = manager.get_task(task.id)
        assert after is not None
        assert after.ball is Ball.HUMAN
        assert "https://claude.ai/code/" in (after.ball_prompt or "")


class TestTheBatchGroupRunsToCompletion:
    def test_it_finishes_and_writes_a_terminal_entry(
        self,
        cli_shim: Path,
        home: Path,
        project_root: Path,
        manager: TaskManager,
        task,
    ) -> None:
        """``review`` is the example's only batch group, and batch has its own failure mode.

        A batch runner missing ``-p`` blocks forever on a session that nothing polls,
        which is the mirror image of the session defect.
        """
        runner = dispatcher(home, project_root, manager, "review")

        handle = runner.start(task, actor="Jeff Posey", caused_by=1)
        assert handle.mode is DispatchMode.BATCH
        assert handle.supervisor is not None
        handle.supervisor.join(timeout=60)
        assert not handle.supervisor.is_alive(), "the batch supervisor never finished"

        assert "-p" in launches(cli_shim)[0]
        after = manager.get_task(task.id)
        assert after is not None
        assert [e for e in after.log if e.type is LogEntryType.DISPATCH_RESULT]


class TestTheExampleAndAgentJobsDoNotBothSupplyTheSameFlag:
    """A runner that writes a flag AgentJobs already writes gets two of it.

    ``-w`` is the one that bites: two worktree flags means one run in two worktrees, and
    nothing about the argv looks wrong until it happens.
    """

    @pytest.mark.parametrize("group", SESSION_GROUPS + ["review"])
    def test_the_worktree_and_permission_flags_appear_exactly_once(
        self,
        group: str,
        cli_shim: Path,
        home: Path,
        project_root: Path,
        manager: TaskManager,
    ) -> None:
        runner = dispatcher(home, project_root, manager, group)

        argv = runner.build_argv("task-001-example", "run_deadbeef")

        for flag in ("-w", "--worktree", "--permission-mode", "--settings", "--tools"):
            assert argv.count(flag) <= 1, f"{flag} appears more than once in {argv}"

    def test_the_shipped_runners_and_groups_are_what_the_test_ran(self, home: Path) -> None:
        """The fixture edits the switch and the projects entry, and nothing else."""
        shipped = EXAMPLE_CONFIG[
            EXAMPLE_CONFIG.index("runners:") : EXAMPLE_CONFIG.index("# ----- projects:")
        ]
        assert shipped in dispatch_config_path(home).read_text(encoding="utf-8")


class TestEveryGroupResolvesToARunnerThisMachineCouldStart:
    def test_no_group_in_the_example_is_dead_on_arrival(self, cli_shim: Path, home: Path) -> None:
        """A group whose every member is skipped refuses; the example must not ship one."""
        config = load_dispatch_config(home)
        assert config is not None

        for group in config.runner_groups:
            resolution = assert_dispatch_permitted(PROJECT_ID, home, group=group)
            assert resolution.selection is not None
            assert resolution.selection.group == group
            assert resolution.runner.mode in (RunnerMode.SESSION, RunnerMode.BATCH)


# ----- the half a shim cannot answer ------------------------------------------

_FLAG = re.compile(r"^-{1,2}[a-zA-Z]")


def example_flags_by_program() -> Dict[str, List[str]]:
    """Every flag the example ships, grouped by the program it is handed to."""
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    by_program: Dict[str, List[str]] = {}
    for definition in (raw.get("runners") or {}).values():
        argv = list(definition["argv"])
        flags = [element for element in argv[1:] if _FLAG.match(element)]
        by_program.setdefault(argv[0], []).extend(flags)
    return by_program


class TestTheFlagsExistOnTheInstalledCli:
    """Check the example against the real CLI, wherever the real CLI is installed.

    This is the check the shim structurally cannot make, and it is the one that catches
    "a flag that does not exist" -- the second half of task-177's review. It **skips**
    where the named program is absent, which is every CI machine and no machine that
    could reasonably be editing the example.
    """

    @pytest.mark.parametrize("program", sorted(example_flags_by_program()))
    def test_every_flag_appears_in_that_programs_own_help(self, program: str) -> None:
        if shutil.which(program) is None:
            pytest.skip(f"{program} is not installed here, so its help cannot be read")

        completed = subprocess.run(
            [shutil.which(program) or program, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        help_text = f"{completed.stdout}\n{completed.stderr}"

        missing = [
            flag
            for flag in example_flags_by_program()[program]
            if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)
        ]
        assert not missing, (
            f"{program} does not document {missing}. The example ships flags that were "
            "run against the CLI, not read off a design document."
        )
