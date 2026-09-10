"""Instantiating a playbook: what gets created, what gets dispatched, what refuses.

``docs/playbooks-design.md`` §4 and the run half of §7. The suite is organised around
the four things that can go wrong rather than around the code's shape:

- the wrong *task* is created or dispatched, including an invocation whose shape
  disagrees with the playbook's ``target`` (``TestProjectTarget``, ``TestTaskTarget``);
- the brief leaks into a place a copy of it would then diverge from
  (``TestPointerNotCopy``);
- a gate that should have refused does not (``TestGatesStillBind``);
- something that cannot be run is run anyway (``TestUnrunnable``).

The runner is a script that exits immediately: what is under test is *whether* a run
starts, against what, and with which prompt -- not what the run then does.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from agentjobs.dispatch.config import DispatchDisabledError
from agentjobs.dispatch.guards import (
    AuthorizerNotHumanError,
    CausingActorNotHumanError,
    ConcurrencyLimitError,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority
from agentjobs.playbooks.pointer import hash_text, pointer_for
from agentjobs.playbooks.run import (
    PlaybookDispatchRefused,
    PlaybookRunError,
    render_title,
    run_playbook,
)
from agentjobs.projects import Project
from support import task_store

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

BRIEF_SENTENCE = "Sweep the open corpus for duplicates and say what you found."
"""A sentence that appears only in a playbook body.

Every "the brief is not copied" assertion searches for this string. A distinctive
sentence rather than a substring of the description, because a description legitimately
*does* reach the run task and a shared phrase would make the assertion pass for the
wrong reason.
"""

PROJECT_PLAYBOOK = f"""---
name: groom
description: Find duplicate and superseded tasks and propose closures.
target: project
difficulty: hard
verbs: [log, handoff, close]
gates:
  - before: close
    what: A human approved the closure list, as recorded on the run task.
run_task:
  title: Groom the {{project}} backlog
  category: meta
  priority: high
  tags: [grooming, playbook]
  acceptance:
    - text: The closure proposal was recorded before anything closed.
    - text: Nothing outside the approved list was closed.
      verify: pytest tests/test_nothing.py
---

# Groom

{BRIEF_SENTENCE}
"""

TASK_PLAYBOOK = f"""---
name: flesh-out
description: Write a full spec onto a thin task, then park it at human review.
target: task
difficulty: hard
verbs: [update, log, handoff]
gates: []
---

# Flesh out

{BRIEF_SENTENCE}
"""


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway AgentJobs home, so nothing here touches a real one."""
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


@pytest.fixture
def fake_runner(tmp_path: Path) -> Path:
    """A runner that exits immediately. What it does is not what these tests measure."""
    script = tmp_path / "runner.py"
    script.write_text("print('started')\n", encoding="utf-8")
    return script


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A registered project with a clean git tree, an actor vocabulary and playbooks."""
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    playbooks = root / "playbooks"
    playbooks.mkdir()
    (playbooks / "groom.md").write_text(PROJECT_PLAYBOOK, encoding="utf-8")
    (playbooks / "flesh-out.md").write_text(TASK_PLAYBOOK, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(task_store(project.root / "tasks"))


def write_dispatch_config(
    home: Path,
    fake_runner: Path,
    *,
    enabled: bool = True,
    limits: Optional[Dict[str, object]] = None,
) -> Path:
    """A machine-local dispatch config that permits 'sandbox' unless told otherwise."""
    config: Dict[str, object] = {
        "version": 1,
        "enabled": enabled,
        "runners": {
            "fake": {"argv": [sys.executable, str(fake_runner), "{prompt}"], "actor": "claude"}
        },
        "projects": {"sandbox": {"enabled": True, "runner": "fake", "require_clean_tree": False}},
    }
    if limits is not None:
        config["limits"] = limits
    path = home / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def dispatchable(home: Path, fake_runner: Path) -> Path:
    """The machine is configured to dispatch this project."""
    return write_dispatch_config(home, fake_runner)


@pytest.fixture
def thin_task(manager: TaskManager):
    """A ready task whose newest log entry was written by a human."""
    task = manager.create_task(
        title="Thin",
        category="general",
        summary="Not much here.",
        description="Barely a sentence.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    return manager.add_log_entry(
        task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Please flesh this out."
    )


def go(manager, project, home, name, **kwargs):
    """Call the run path the way the CLI and the endpoint both do."""
    kwargs.setdefault("created_by", "Jeff Posey")
    return run_playbook(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        name=name,
        home=home,
        **kwargs,
    )


def settle(handle) -> None:
    """Let a batch supervisor finish so it does not race the next assertion."""
    if handle.supervisor is not None:
        handle.supervisor.join(timeout=30)


def dispatch_entry(manager: TaskManager, task_id: str):
    """The newest ``dispatch`` entry on a task."""
    task = manager.get_task(task_id)
    assert task is not None
    entries = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
    assert entries, f"{task_id} has no dispatch entry"
    return entries[-1]


def prompt_of(manager: TaskManager, task_id: str) -> str:
    """The prompt the run was started with, read off the recorded argv.

    Read from the record rather than from the handle on purpose: the record is what a
    later reader has, and an assertion about what an agent was told should be made
    against the thing that outlives the process.
    """
    argv = dispatch_entry(manager, task_id).data["argv"]
    return str(argv[-1])


def task_count(manager: TaskManager) -> int:
    return len(manager.storage.list_tasks())


# ----- target: project --------------------------------------------------------


class TestProjectTarget:
    """A project-target run creates its run task from frontmatter, then dispatches it."""

    def test_it_creates_a_ready_run_task_and_dispatches_it(
        self, manager, project, home, dispatchable
    ):
        result = go(manager, project, home, "groom")
        settle(result.handle)

        assert result.created_run_task is True
        task = manager.get_task(result.task_id)
        assert task is not None
        assert task.lifecycle is Lifecycle.ACTIVE  # claimed by the dispatcher
        assert dispatch_entry(manager, result.task_id).data["run_id"] == result.handle.run_id

    def test_the_run_task_carries_the_frontmatter_defaults(
        self, manager, project, home, dispatchable
    ):
        result = go(manager, project, home, "groom")
        settle(result.handle)
        task = manager.get_task(result.task_id)
        assert task is not None

        assert task.title == "Groom the sandbox backlog"
        assert task.category == "meta"
        assert task.priority is Priority.HIGH
        assert task.tags == ["grooming", "playbook"]
        assert [criterion.id for criterion in task.acceptance] == ["ac-1", "ac-2"]
        assert task.acceptance[0].text.startswith("The closure proposal")
        assert task.acceptance[1].verify == "pytest tests/test_nothing.py"

    def test_the_run_task_points_at_the_playbook_rather_than_quoting_it(
        self, manager, project, home, dispatchable
    ):
        result = go(manager, project, home, "groom")
        settle(result.handle)
        task = manager.get_task(result.task_id)
        assert task is not None

        assert [pointer.path for pointer in task.spec.context] == ["playbooks/groom.md"]
        assert "playbooks/groom.md" in task.spec.description
        assert BRIEF_SENTENCE not in task.spec.description

    def test_the_creating_human_is_who_the_dispatch_is_clocked_to(
        self, manager, project, home, dispatchable
    ):
        """The creation entry is the authorisation -- no second entry is written.

        This is the whole of the project-target authorisation story (§7.1). If a future
        change starts writing an authorising note as well, ``caused_by`` moves off the
        creation entry and this fails.
        """
        result = go(manager, project, home, "groom")
        settle(result.handle)
        task = manager.get_task(result.task_id)
        assert task is not None

        entry = dispatch_entry(manager, result.task_id)
        causing = next(item for item in task.log if item.id == entry.data["caused_by"])
        assert causing.actor == "Jeff Posey"
        assert causing.type is LogEntryType.TRANSITION
        assert "Created ready by Jeff Posey" in causing.body

    def test_naming_a_task_is_refused_and_creates_nothing(
        self, manager, project, home, dispatchable
    ):
        before = task_count(manager)
        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "groom", task_id="task-001")
        assert refused.value.reason == "target_mismatch"
        assert task_count(manager) == before


# ----- target: task -----------------------------------------------------------


class TestTaskTarget:
    """A task-target run dispatches the task itself and creates no second record."""

    def test_it_dispatches_the_named_task_and_creates_no_run_task(
        self, manager, project, home, dispatchable, thin_task
    ):
        before = task_count(manager)
        result = go(manager, project, home, "flesh-out", task_id=thin_task.id, created_by=None)
        settle(result.handle)

        assert result.created_run_task is False
        assert result.task_id == thin_task.id
        assert task_count(manager) == before

    def test_omitting_the_task_is_refused(self, manager, project, home, dispatchable):
        before = task_count(manager)
        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "flesh-out")
        assert refused.value.reason == "target_mismatch"
        assert task_count(manager) == before


# ----- the pointer ------------------------------------------------------------


class TestPointerNotCopy:
    """The record pins which brief ran; nothing anywhere copies it (§4.2-4.3)."""

    def test_the_dispatch_entry_names_the_playbook_and_its_hash(
        self, manager, project, home, dispatchable
    ):
        result = go(manager, project, home, "groom")
        settle(result.handle)

        data = dispatch_entry(manager, result.task_id).data
        expected = hashlib.sha256(PROJECT_PLAYBOOK.encode("utf-8")).hexdigest()
        assert data["playbook"] == "groom"
        assert data["playbook_hash"] == f"sha256:{expected}"

    def test_an_ordinary_dispatch_records_neither_field(
        self, manager, project, home, dispatchable, thin_task
    ):
        """Absent rather than null, so a corpus of pre-playbook records is unchanged."""
        from agentjobs.dispatch.guards import DispatchRequest, dispatch_task

        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=thin_task.id),
            home=home,
        )
        settle(handle)
        data = dispatch_entry(manager, thin_task.id).data
        assert "playbook" not in data
        assert "playbook_hash" not in data

    def test_the_prompt_gains_one_pointer_line_and_not_the_brief(
        self, manager, project, home, dispatchable
    ):
        result = go(manager, project, home, "groom")
        settle(result.handle)
        prompt = prompt_of(manager, result.task_id)

        assert "playbooks/groom.md" in prompt
        assert result.pointer.short_digest in prompt
        assert BRIEF_SENTENCE not in prompt

    def test_the_task_target_prompt_carries_the_pointer_too(
        self, manager, project, home, dispatchable, thin_task
    ):
        result = go(manager, project, home, "flesh-out", task_id=thin_task.id, created_by=None)
        settle(result.handle)
        prompt = prompt_of(manager, thin_task.id)

        assert "playbooks/flesh-out.md" in prompt
        assert BRIEF_SENTENCE not in prompt

    def test_the_pointer_line_is_all_that_changes_about_the_stub(
        self, manager, project, home, dispatchable, thin_task
    ):
        """The stub itself is untouched: same text, one sentence appended.

        Asserted as a prefix rather than by counting characters, because the point is
        that a playbook run is an ordinary dispatch plus a pointer -- not a dispatch
        with a different prompt.
        """
        from agentjobs.dispatch.config import assert_dispatch_permitted
        from agentjobs.dispatch.runner import DispatchRunner

        resolution = assert_dispatch_permitted("sandbox", home)
        plain = DispatchRunner(
            manager=manager, resolution=resolution, project_root=project.root, home=home
        ).build_prompt(thin_task.id, "run_x")

        result = go(manager, project, home, "flesh-out", task_id=thin_task.id, created_by=None)
        settle(result.handle)
        decorated = prompt_of(manager, thin_task.id)

        stub, _, appended = decorated.partition(" Your brief is the playbook")
        # The run id differs between the two, and it is the last token of the stub.
        assert stub.split("Dispatch run id:")[0] == plain.split("Dispatch run id:")[0]
        assert appended

    def test_the_hash_is_of_the_file_and_moves_when_the_file_does(self, project):
        from agentjobs.playbooks import read_playbook

        first = pointer_for(
            read_playbook(project.playbooks_dir(), "groom"), project_root=project.root
        )
        path = project.playbooks_dir() / "groom.md"
        path.write_text(PROJECT_PLAYBOOK + "\nOne more rule.\n", encoding="utf-8")
        second = pointer_for(
            read_playbook(project.playbooks_dir(), "groom"), project_root=project.root
        )

        assert first.digest != second.digest
        assert first.path == second.path == "playbooks/groom.md"

    def test_line_endings_are_not_a_difference(self):
        """A checkout with CRLF is the same brief, so it must hash the same."""
        assert hash_text("a\r\nb\n") == hash_text("a\nb\n")


# ----- the gates --------------------------------------------------------------


class TestGatesStillBind:
    """Naming a playbook opens nothing (§6.3). Each gate refuses exactly as before."""

    def test_dispatch_disabled_refuses_and_creates_no_run_task(
        self, manager, project, home, fake_runner
    ):
        write_dispatch_config(home, fake_runner, enabled=False)
        before = task_count(manager)

        with pytest.raises(DispatchDisabledError):
            go(manager, project, home, "groom")

        # The pre-flight is why this matters: a refusal that could never have started a
        # run must not leave a run task in the backlog explaining nothing.
        assert task_count(manager) == before

    def test_an_agent_cannot_be_the_creating_human(self, manager, project, home, dispatchable):
        before = task_count(manager)
        with pytest.raises(AuthorizerNotHumanError):
            go(manager, project, home, "groom", created_by="claude")
        assert task_count(manager) == before

    def test_an_agents_entry_cannot_clock_a_task_target_run(
        self, manager, project, home, dispatchable, thin_task
    ):
        manager.add_log_entry(
            thin_task.id, actor="claude", type=LogEntryType.PROGRESS, body="Had a look."
        )
        with pytest.raises(CausingActorNotHumanError):
            go(manager, project, home, "flesh-out", task_id=thin_task.id, created_by=None)

    def test_a_full_machine_refuses_and_says_which_run_task_it_left(
        self, manager, project, home, fake_runner
    ):
        """The one refusal that can arrive after the run task exists.

        Nothing is deleted to tidy up, so the refusal has to name what it left behind --
        otherwise a ``ready`` task appears in the backlog with no account of itself.
        """
        write_dispatch_config(home, fake_runner, limits={"max_concurrent_runs": 1})
        first = go(manager, project, home, "groom")
        settle(first.handle)
        first.handle.directory.update_meta(status="running")

        before = task_count(manager)
        with pytest.raises(PlaybookDispatchRefused) as refused:
            go(manager, project, home, "groom")

        assert isinstance(refused.value.cause, ConcurrencyLimitError)
        assert refused.value.reason == "concurrency_limit"
        assert task_count(manager) == before + 1
        assert refused.value.run_task_id in str(refused.value)
        assert manager.get_task(refused.value.run_task_id) is not None

    def test_a_project_target_run_with_nobody_named_is_refused(
        self, manager, project, home, dispatchable
    ):
        before = task_count(manager)
        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "groom", created_by=None)
        assert refused.value.reason == "no_authorizing_human"
        assert task_count(manager) == before

    def test_the_pointer_is_inert_in_the_guard_chain(self):
        """No gate reads the playbook, and this is the check that keeps it that way.

        A playbook is repository content. The moment a guard consults one, a `git clone`
        can influence what executes on a machine -- dispatch gate 2, restated for
        playbooks as design §6.3.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src/agentjobs/dispatch/guards.py"
        ).read_text(encoding="utf-8")
        body = source.split("def dispatch_task(", 1)[1]
        uses = [line for line in body.splitlines() if "request.playbook" in line]
        assert uses == ["            playbook=request.playbook,"], uses


# ----- what cannot be run -----------------------------------------------------


class TestUnrunnable:
    """Files and names ``playbook run`` refuses before anything is written."""

    def test_an_unknown_name(self, manager, project, home, dispatchable):
        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "nope")
        assert refused.value.reason == "unknown_playbook"

    def test_a_name_that_is_a_path(self, manager, project, home, dispatchable):
        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "../../etc/passwd")
        assert refused.value.reason == "invalid_playbook_name"

    def test_a_reactive_playbook_cannot_be_run_because_it_cannot_be_read(
        self, manager, project, home, dispatchable
    ):
        """`kind: reactive` is refused, and the refusal is structural.

        The reactive category was withdrawn by design decision P8 on 2026-08-21, so
        there is no ``kind`` field and the contract model forbids unknown keys. A file
        declaring one therefore never loads, and a brief that never loads can never be
        run -- on any surface, including ones nobody has written yet. That is a stronger
        guarantee than an enum check in this module would have been, and it is why no
        ``reactive`` vocabulary exists in the codebase to check against.
        """
        (project.playbooks_dir() / "watch.md").write_text(
            PROJECT_PLAYBOOK.replace("name: groom", "name: watch\nkind: reactive"),
            encoding="utf-8",
        )
        before = task_count(manager)

        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "watch")

        assert refused.value.reason == "invalid_playbook"
        assert "kind" in str(refused.value)
        assert task_count(manager) == before

    def test_a_project_playbook_with_no_run_task_block(self, manager, project, home, dispatchable):
        (project.playbooks_dir() / "bare.md").write_text(
            "---\n"
            "name: bare\n"
            "description: A project playbook that forgot its run_task block.\n"
            "target: project\n"
            "difficulty: routine\n"
            "---\n\nNothing to create a task from.\n",
            encoding="utf-8",
        )
        before = task_count(manager)

        with pytest.raises(PlaybookRunError) as refused:
            go(manager, project, home, "bare")

        assert refused.value.reason == "no_run_task_defaults"
        assert task_count(manager) == before


# ----- small pieces -----------------------------------------------------------


class TestTitleTemplating:
    def test_it_fills_the_two_placeholders(self):
        rendered = render_title(
            "{playbook} the {project} backlog", project_id="aj", playbook_name="groom"
        )
        assert rendered == "groom the aj backlog"

    def test_a_brace_it_does_not_know_survives_untouched(self):
        """A title is authored prose; ``str.format`` would raise on this."""
        assert (
            render_title("Groom {everything}", project_id="aj", playbook_name="groom")
            == "Groom {everything}"
        )
