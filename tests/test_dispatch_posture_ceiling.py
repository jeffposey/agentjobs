"""Where a run's posture comes from, and what the project's ceiling does to it (task-308).

The load-bearing test in this file is ``TestAgentWrittenPostureCannotWiden``. Everything
else establishes a rule that could reasonably be tuned; that one is the reason a task
record -- a git-tracked file any agent can write, including the agent working that task
-- is safe to take a posture from at all. It asserts on the **argv the process would be
started with**, not on the resolver's return value, because the resolver agreeing with
itself proves nothing about what a run was actually permitted to do.

The dispatch-level tests reuse ``test_dispatch_guards``' harness, which starts a runner
that exits immediately: what is under test is which envelope a run gets, never what the
run then does with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchConfigError,
    Posture,
    PostureAboveCeilingError,
    PostureSource,
    ProjectDispatchSettings,
    load_dispatch_config,
    resolve_posture,
)
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.runner import RunDirectory
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchPosture, Lifecycle, LogEntryType
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

PROJECT_CONFIG: Dict[str, object] = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


@pytest.fixture
def fake_runner(tmp_path: Path) -> Path:
    script = tmp_path / "runner.py"
    script.write_text("print('started')\n", encoding="utf-8")
    return script


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(TaskStorage(project.root / "tasks"))


def write_config(
    home: Path,
    fake_runner: Path,
    **project_overrides: object,
) -> Path:
    """A machine-local config permitting 'sandbox', with the project entry overridable."""
    entry: Dict[str, object] = {
        "enabled": True,
        "runner": "fake",
        "require_clean_tree": True,
    }
    entry.update(project_overrides)
    config = {
        "version": 1,
        "enabled": True,
        "api_base": "http://127.0.0.1:9/",
        "runners": {
            "fake": {"argv": [sys.executable, str(fake_runner), "{prompt}"], "actor": "claude"}
        },
        "projects": {"sandbox": entry},
    }
    path = home / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def ready_task(manager: TaskManager):
    """A ready task whose newest entry is a human's, so the human-clocked gate opens."""
    task = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    return manager.add_log_entry(
        task.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Go ahead."
    )


def start(
    manager: TaskManager,
    project: Project,
    home: Path,
    task_id: str,
    *,
    posture: Optional[Posture] = None,
):
    """Call the guard chain the way the endpoint and the CLI both do."""
    return dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(task_id=task_id, posture=posture),
        home=home,
        # An address is supplied so the reachability probe is skipped: this file is
        # about postures, and a loopback round trip is a different test's subject.
        api_base="http://127.0.0.1:9/",
    )


def settings(**kwargs: object) -> ProjectDispatchSettings:
    return ProjectDispatchSettings(project_id="sandbox", **kwargs)  # type: ignore[arg-type]


def dispatch_entry(manager: TaskManager, task_id: str) -> Dict[str, object]:
    """The newest ``dispatch`` entry's data payload."""
    task = manager.get_task(task_id)
    assert task is not None
    entries = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
    assert entries, "no dispatch entry was written"
    return dict(entries[-1].data)


# ----- the width order --------------------------------------------------------


class TestWidthOrder:
    """``read_only`` < ``supervised`` < ``auto`` < ``autonomous``, and why not ``<=``."""

    def test_ranks_run_narrowest_to_widest(self) -> None:
        assert [posture.value for posture in sorted(Posture, key=lambda p: p.rank)] == [
            "read_only",
            "supervised",
            "auto",
            "autonomous",
        ]

    def test_supervised_is_narrower_than_auto(self) -> None:
        # The one that surprises people. Supervised parks on anything outside nine
        # allow-listed prefixes and an unattended run has nobody to answer, so its
        # *unattended* envelope -- the only one a ceiling is about -- is the smaller.
        assert Posture.SUPERVISED.within(Posture.AUTO)
        assert not Posture.AUTO.within(Posture.SUPERVISED)

    def test_within_is_reflexive(self) -> None:
        for posture in Posture:
            assert posture.within(posture)

    def test_string_comparison_would_have_got_it_backwards(self) -> None:
        """The reason ``within`` exists rather than an ``__le__`` override.

        ``Posture`` inherits ``str``, so it already has comparison operators and they
        compare spelling. This asserts the trap is real, so that anyone who later
        "simplifies" ``within`` into ``<=`` has a failing test explaining why not.
        """
        # Alphabetical, so `read_only` sorts above `autonomous` -- exactly backwards,
        # and the narrowest posture in the enum would clear every ceiling.
        assert Posture.READ_ONLY > Posture.AUTONOMOUS
        assert Posture.READ_ONLY.within(Posture.AUTONOMOUS)
        assert not Posture.AUTONOMOUS.within(Posture.READ_ONLY)


# ----- the ceiling on the project ---------------------------------------------


class TestProjectCeiling:
    def test_absent_ceiling_is_the_default_posture(self) -> None:
        """The only default that cannot silently widen a machine on upgrade."""
        assert settings(posture=Posture.AUTO).ceiling is Posture.AUTO
        assert settings(posture=Posture.READ_ONLY).ceiling is Posture.READ_ONLY

    def test_a_declared_ceiling_wins(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        assert raised.ceiling is Posture.AUTONOMOUS

    def test_offerable_postures_stop_at_the_ceiling(self) -> None:
        assert [p.value for p in settings(posture=Posture.AUTO).offerable_postures()] == [
            "read_only",
            "supervised",
            "auto",
        ]

    def test_offerable_postures_include_the_ceiling_itself(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        assert [p.value for p in raised.offerable_postures()] == [
            "read_only",
            "supervised",
            "auto",
            "autonomous",
        ]


class TestCeilingParsing:
    def test_max_posture_is_read_from_the_file(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, posture="auto", max_posture="autonomous")
        config = load_dispatch_config(home)
        assert config is not None
        assert config.projects["sandbox"].ceiling is Posture.AUTONOMOUS

    def test_an_unknown_ceiling_is_refused(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, max_posture="yolo")
        with pytest.raises(DispatchConfigError, match="max_posture"):
            load_dispatch_config(home)

    def test_a_default_wider_than_its_own_ceiling_is_refused(
        self, home: Path, fake_runner: Path
    ) -> None:
        """Refused rather than clamped: both halves were typed by the same person.

        Unlike the two sources the ceiling exists to bound, there is nobody here to
        protect from anybody, so quietly running their default at their ceiling would be
        the tool deciding which of two contradictory lines they meant.
        """
        write_config(home, fake_runner, posture="autonomous", max_posture="auto")
        with pytest.raises(DispatchConfigError, match="wider than"):
            load_dispatch_config(home)

    def test_supervised_default_under_an_auto_ceiling_is_fine(
        self, home: Path, fake_runner: Path
    ) -> None:
        """Guards the width order at the parse boundary, where getting it backwards
        would refuse a perfectly ordinary config."""
        write_config(home, fake_runner, posture="supervised", max_posture="auto")
        config = load_dispatch_config(home)
        assert config is not None
        assert config.projects["sandbox"].posture is Posture.SUPERVISED


# ----- precedence (sc-3) ------------------------------------------------------


class TestPrecedence:
    """Most-specific-wins: dispatch choice > epic > task record > project default."""

    def test_nothing_named_gives_the_project_default(self) -> None:
        resolved = resolve_posture(settings(posture=Posture.AUTO))
        assert resolved.posture is Posture.AUTO
        assert resolved.source is PostureSource.PROJECT
        assert not resolved.clamped

    def test_the_task_record_beats_the_project_default(self) -> None:
        resolved = resolve_posture(settings(posture=Posture.AUTO), task=Posture.SUPERVISED)
        assert resolved.posture is Posture.SUPERVISED
        assert resolved.source is PostureSource.TASK

    def test_a_dispatch_time_choice_beats_the_task_record(self) -> None:
        resolved = resolve_posture(
            settings(posture=Posture.AUTO),
            task=Posture.READ_ONLY,
            requested=Posture.SUPERVISED,
        )
        assert resolved.posture is Posture.SUPERVISED
        assert resolved.source is PostureSource.DISPATCH

    def test_a_dispatch_time_choice_may_be_narrower_than_the_task_record(self) -> None:
        """Most-specific-wins is not most-permissive-wins, and the difference matters."""
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(raised, task=Posture.AUTONOMOUS, requested=Posture.READ_ONLY)
        assert resolved.posture is Posture.READ_ONLY

    def test_a_task_posture_within_the_ceiling_is_honoured_upward(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(raised, task=Posture.AUTONOMOUS)
        assert resolved.posture is Posture.AUTONOMOUS
        assert not resolved.clamped


class TestAnInheritedPostureSitsBetweenDispatchAndTheTaskRecord:
    """Where the epic's choice lands in the order, and why it is above the record (task-316).

    The placement is the whole decision. A field on the child's own record is narrower in
    scope, so the "most specific wins" phrasing would seem to put it first; it does not,
    because an inherited posture is a *person's* choice made when they authorised the
    epic, and the record's field is a git-tracked value any agent can write. Letting the
    field win would mean "I dispatched the epic autonomous" quietly meant something
    different per child.
    """

    def test_an_inherited_posture_beats_the_project_default(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(raised, inherited=Posture.AUTONOMOUS)
        assert resolved.posture is Posture.AUTONOMOUS
        assert resolved.source is PostureSource.EPIC
        assert not resolved.clamped

    def test_an_inherited_posture_beats_the_task_record(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(raised, task=Posture.SUPERVISED, inherited=Posture.AUTONOMOUS)
        assert resolved.posture is Posture.AUTONOMOUS
        assert resolved.source is PostureSource.EPIC

    def test_a_dispatch_time_choice_beats_an_inherited_one(self) -> None:
        """What makes ``dispatch walk --posture`` mean anything."""
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(
            raised, inherited=Posture.AUTONOMOUS, requested=Posture.READ_ONLY
        )
        assert resolved.posture is Posture.READ_ONLY
        assert resolved.source is PostureSource.DISPATCH

    def test_an_inherited_posture_may_be_narrower_than_the_task_record(self) -> None:
        """Inheritance is not "take the wider of the two"; it is "the person decided"."""
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        resolved = resolve_posture(raised, task=Posture.AUTONOMOUS, inherited=Posture.READ_ONLY)
        assert resolved.posture is Posture.READ_ONLY
        assert resolved.source is PostureSource.EPIC

    def test_an_inherited_posture_above_the_ceiling_is_refused_not_clamped(self) -> None:
        """It is a dispatch-time choice one generation up, so it is treated as one.

        Only reachable when somebody lowers ``max_posture`` while a walk is running: the
        parent's own dispatch checked this same ceiling. Stopping loudly is the honest
        answer, because the authority the walk is standing on no longer fits.
        """
        with pytest.raises(PostureAboveCeilingError) as caught:
            resolve_posture(settings(posture=Posture.AUTO), inherited=Posture.AUTONOMOUS)
        message = str(caught.value)
        assert "epic" in message, "the refusal must say the posture was inherited"
        assert "max_posture" in message, "the refusal must name where the cap is set"

    def test_the_source_is_recorded_as_the_epic_rather_than_as_a_dispatch(self) -> None:
        """ac-3: a child that merged unreviewed is traceable to the act that bought it.

        ``dispatch`` would say somebody chose this envelope for *this* run, which is
        false and points a reader at the wrong record; ``project`` would say
        ``dispatch.yaml`` decided, which is what the defect this fixes actually wrote.
        """
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        data = resolve_posture(raised, inherited=Posture.AUTONOMOUS).as_data()
        assert data == {"posture_source": "epic", "posture_ceiling": "autonomous"}

    def test_it_describes_itself_for_a_human_reading_a_log(self) -> None:
        raised = settings(posture=Posture.AUTO, max_posture=Posture.AUTONOMOUS)
        assert (
            resolve_posture(raised, inherited=Posture.AUTONOMOUS).describe()
            == "posture autonomous (from the epic)"
        )


class TestTheCeilingClampsAndRefuses:
    """The asymmetry: a task record is clamped, a dispatch-time request is refused."""

    def test_a_task_posture_above_the_ceiling_is_clamped(self) -> None:
        resolved = resolve_posture(settings(posture=Posture.AUTO), task=Posture.AUTONOMOUS)
        assert resolved.posture is Posture.AUTO
        assert resolved.source is PostureSource.TASK
        assert resolved.requested is Posture.AUTONOMOUS
        assert resolved.clamped

    def test_clamping_rather_than_refusing_keeps_the_task_dispatchable(self) -> None:
        """The reason the task record is not refused like a dispatch-time request.

        Refusing would hand every agent a denial of service on its own task: write an
        over-ceiling value into a file it can already write, and every future dispatch
        of that task fails. This asserts the value is still produced, not raised.
        """
        assert (
            resolve_posture(settings(posture=Posture.AUTO), task=Posture.AUTONOMOUS).posture
            is Posture.AUTO
        )

    def test_a_dispatch_time_request_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(PostureAboveCeilingError) as caught:
            resolve_posture(settings(posture=Posture.AUTO), requested=Posture.AUTONOMOUS)
        message = str(caught.value)
        assert "max_posture" in message, "the refusal must name where the cap is set"
        assert "auto" in message, "the refusal must name the ceiling it hit"

    def test_the_refusal_carries_its_own_reason_code(self) -> None:
        with pytest.raises(PostureAboveCeilingError) as caught:
            resolve_posture(settings(posture=Posture.AUTO), requested=Posture.AUTONOMOUS)
        assert caught.value.reason == "posture_above_ceiling"

    def test_a_project_default_above_its_ceiling_is_clamped_in_code(self) -> None:
        """Unreachable through the parser, which refuses it -- this is the code path for
        a settings object built by hand, and it must fail closed rather than open."""
        incoherent = settings(posture=Posture.AUTONOMOUS, max_posture=Posture.AUTO)
        resolved = resolve_posture(incoherent)
        assert resolved.posture is Posture.AUTO
        assert resolved.requested is Posture.AUTONOMOUS


class TestTheAccount:
    """What ``ResolvedPosture`` puts on a record."""

    def test_an_unclamped_resolution_records_no_request(self) -> None:
        data = resolve_posture(settings(posture=Posture.AUTO)).as_data()
        assert data == {"posture_source": "project", "posture_ceiling": "auto"}

    def test_a_clamped_resolution_records_what_asked(self) -> None:
        data = resolve_posture(settings(posture=Posture.AUTO), task=Posture.AUTONOMOUS).as_data()
        assert data == {
            "posture_source": "task",
            "posture_ceiling": "auto",
            "posture_requested": "autonomous",
        }

    def test_as_data_never_repeats_the_posture_itself(self) -> None:
        """It is already a top-level field on the entry; two copies could disagree."""
        assert "posture" not in resolve_posture(settings(posture=Posture.AUTO)).as_data()


# ----- the whole chain (sc-4, sc-5) -------------------------------------------


class TestAgentWrittenPostureCannotWiden:
    """sc-5. The one test in this file that is a security property.

    An agent writes ``autonomous`` onto its own task record -- which it can do, because
    the record is a git-tracked file it has write access to -- on a project whose
    machine-local config never raised its ceiling. The run must come out at the
    project's posture, and the *process* must be started with the narrow flags.
    """

    def test_the_run_gets_the_project_posture_not_the_task_one(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, posture="auto")  # no max_posture: ceiling is auto
        manager.update_task(ready_task.id, actor="claude", posture=DispatchPosture.AUTONOMOUS.value)

        handle = start(manager, project, home, ready_task.id)

        meta = RunDirectory(handle.directory.path).read_meta()
        assert meta["posture"] == "auto"

    def test_the_process_was_never_started_with_bypass_permissions(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Asserted on the recorded argv, not on the resolver.

        The resolver agreeing with itself proves nothing about what the run was
        permitted to do; ``argv`` is what the operating system was handed.
        """
        write_config(home, fake_runner, posture="auto")
        manager.update_task(ready_task.id, actor="claude", posture=DispatchPosture.AUTONOMOUS.value)

        start(manager, project, home, ready_task.id)

        argv = dispatch_entry(manager, ready_task.id)["argv"]
        assert "bypassPermissions" not in " ".join(argv)  # type: ignore[arg-type]

    def test_the_record_says_it_was_cut_down_and_by_what(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """sc-4, in its hardest case: a reader must not have to open a machine-local
        file to find out why the task's own request did not take effect."""
        write_config(home, fake_runner, posture="auto")
        manager.update_task(ready_task.id, actor="claude", posture=DispatchPosture.AUTONOMOUS.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["posture"] == "auto"
        assert data["posture_source"] == "task"
        assert data["posture_ceiling"] == "auto"
        assert data["posture_requested"] == "autonomous"

    def test_raising_the_machine_local_ceiling_is_what_lets_it_through(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The other half of the property: the ceiling, and only the ceiling, is what
        was stopping it. Without this the test above would pass on a broken build that
        ignored the task field entirely."""
        write_config(home, fake_runner, posture="auto", max_posture="autonomous")
        manager.update_task(ready_task.id, actor="claude", posture=DispatchPosture.AUTONOMOUS.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["posture"] == "autonomous"
        assert data["posture_source"] == "task"
        assert "posture_requested" not in data
        assert "bypassPermissions" in " ".join(data["argv"])  # type: ignore[arg-type]


class TestTheDispatchEntryNamesItsSource:
    """sc-4, for the two ordinary cases."""

    def test_an_untouched_task_records_the_project_as_the_source(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, posture="auto")

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["posture"] == "auto"
        assert data["posture_source"] == "project"
        assert "posture_requested" not in data

    def test_a_dispatch_time_choice_records_itself(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, posture="auto")

        start(manager, project, home, ready_task.id, posture=Posture.READ_ONLY)

        data = dispatch_entry(manager, ready_task.id)
        assert data["posture"] == "read_only"
        assert data["posture_source"] == "dispatch"

    def test_a_narrowing_task_posture_needs_no_ceiling_to_be_honoured(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The uncomplicated half of the feature, and the reason to reach for the field
        on most tasks: asking for *less* is safe by construction."""
        write_config(home, fake_runner, posture="auto")
        manager.update_task(ready_task.id, actor="claude", posture=DispatchPosture.READ_ONLY.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["posture"] == "read_only"
        assert data["posture_source"] == "task"


class TestTheDispatchRefusal:
    def test_an_over_ceiling_request_refuses_the_whole_dispatch(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, posture="auto")

        with pytest.raises(PostureAboveCeilingError):
            start(manager, project, home, ready_task.id, posture=Posture.AUTONOMOUS)

    def test_a_refused_dispatch_writes_no_dispatch_entry(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The refusal happens above the run lock and above every write, so a task that
        was refused looks exactly as it did before somebody asked."""
        write_config(home, fake_runner, posture="auto")

        with pytest.raises(PostureAboveCeilingError):
            start(manager, project, home, ready_task.id, posture=Posture.AUTONOMOUS)

        task = manager.get_task(ready_task.id)
        assert task is not None
        assert not [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]


class TestTheTaskFieldIsOptional:
    def test_a_task_without_a_posture_serialises_without_the_key(
        self, manager: TaskManager, ready_task
    ) -> None:
        """``exclude_none`` keeps the field out of every task file that never set it,
        so introducing it rewrites nothing."""
        stored = (manager.storage.tasks_dir / f"{ready_task.id}.yaml").read_text(encoding="utf-8")
        assert "posture:" not in stored

    def test_setting_and_clearing_it_round_trips(self, manager: TaskManager, ready_task) -> None:
        manager.update_task(ready_task.id, posture=DispatchPosture.SUPERVISED.value)
        task = manager.get_task(ready_task.id)
        assert task is not None and task.posture is DispatchPosture.SUPERVISED

        manager.update_task(ready_task.id, posture=None)
        task = manager.get_task(ready_task.id)
        assert task is not None and task.posture is None
