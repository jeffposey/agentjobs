"""Where a run's merge mode comes from, and what the project's ceiling does to it.

task-308 built the precedence and the ceiling; task-602 collapsed four postures into the
one choice a person makes -- ``review`` or ``automerge`` -- and the ceiling into a yes/no.

The load-bearing test in this file is ``TestAgentWrittenMergeModeCannotWiden``.
Everything else establishes a rule that could reasonably be tuned; that one is the reason
a task record -- a file any agent can write, including the agent working that task -- is
safe to take a merge mode from at all. It asserts on the **argv the process would be
started with**, not on the resolver's return value, because the resolver agreeing with
itself proves nothing about what a run was actually permitted to do.

``TestALegacyAutoIsNeverAutomerge`` is the other one that matters: every record written
before task-602 says ``auto`` and means *stop for review*.

The dispatch-level tests reuse ``test_dispatch_guards``' harness, which starts a runner
that exits immediately: what is under test is which mode a run gets, never what the run
then does with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from agentjobs.dispatch.config import (
    AutomergeNotAllowedError,
    DispatchConfigError,
    MergeModeSource,
    ProjectDispatchSettings,
    load_dispatch_config,
    resolve_merge_mode,
)
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.runner import RunDirectory
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    DispatchData,
    Lifecycle,
    LogEntryType,
    MergeMode,
    Task,
    recorded_merge_mode,
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
    return TaskManager(task_store(project.root / "tasks", project_id=project.id))


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
    merge_mode: Optional[MergeMode] = None,
):
    """Call the guard chain the way the endpoint and the CLI both do."""
    return dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(task_id=task_id, merge_mode=merge_mode),
        home=home,
        # An address is supplied so the reachability probe is skipped: this file is
        # about merge modes, and a loopback round trip is a different test's subject.
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


REVIEW, AUTOMERGE = MergeMode.REVIEW, MergeMode.AUTOMERGE


# ----- the two values ---------------------------------------------------------


class TestTheTwoValues:
    def test_there_are_exactly_two_and_neither_is_spelled_auto(self) -> None:
        """``auto`` meant *stop for review* in every record written before task-602."""
        assert [mode.value for mode in MergeMode] == ["review", "automerge"]
        assert "auto" not in {mode.value for mode in MergeMode}

    def test_each_has_the_phrase_every_surface_shows(self) -> None:
        assert REVIEW.phrase == "Hands off for your review"
        assert AUTOMERGE.phrase == "Merges itself on a green gate"


class TestALegacyAutoIsNeverAutomerge:
    """a3. A retired spelling reads as the mode it meant, wherever it can appear."""

    @pytest.mark.parametrize(
        ("legacy", "expected"),
        [
            ("auto", REVIEW),
            ("supervised", REVIEW),
            ("read_only", REVIEW),
            ("autonomous", AUTOMERGE),
        ],
    )
    def test_the_enum_reads_every_retired_spelling(self, legacy: str, expected: MergeMode) -> None:
        assert MergeMode(legacy) is expected

    def test_a_task_record_written_before_the_rename(self) -> None:
        task = Task.model_validate(
            {
                "schema": 2,
                "id": "task-001",
                "title": "Old",
                "created": "2026-09-01T00:00:00Z",
                "updated": "2026-09-01T00:00:00Z",
                "lifecycle": "ready",
                "ball": "agent",
                "ball_reason": "available",
                "priority": "medium",
                "queue_position": 1,
                "category": "general",
                "posture": "auto",
                "spec": {"summary": "s", "description": "d"},
            }
        )
        assert task.merge_mode is REVIEW

    def test_a_dispatch_entry_written_before_the_rename(self) -> None:
        data = DispatchData.model_validate(
            {
                "run_id": "run_1",
                "agent": "claude",
                "runner": "fake",
                "mode": "session",
                "posture": "auto",
                "posture_source": "project",
                "posture_ceiling": "autonomous",
                "trigger": "manual",
                "caused_by": 1,
                "argv": ["x"],
                "cwd": ".",
                "git_head": "abc",
                "delivery": {"channel": "argv", "posture_delivered": True},
            }
        )
        assert data.merge_mode is REVIEW
        assert data.merge_mode_source == "project"
        assert data.allow_automerge is True
        assert data.delivery is not None and data.delivery.merge_mode_delivered is True

    def test_a_run_directory_or_journal_mapping(self) -> None:
        assert recorded_merge_mode({"posture": "auto"}) is REVIEW
        assert recorded_merge_mode({"posture": "autonomous"}) is AUTOMERGE
        assert recorded_merge_mode({"merge_mode": "review", "posture": "autonomous"}) is REVIEW
        assert recorded_merge_mode({}) is None

    def test_a_dispatch_yaml_default(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, posture="auto")
        config = load_dispatch_config(home)
        assert config is not None
        assert config.projects["sandbox"].merge_mode is REVIEW
        assert not config.projects["sandbox"].automerge_allowed

    def test_an_old_config_starts_a_process_that_stops_for_review(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """End to end: an old config saying ``auto`` is review even with automerge allowed."""
        write_config(home, fake_runner, posture="auto", allow_automerge=True)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "review"
        assert "bypassPermissions" not in " ".join(data["argv"])  # type: ignore[arg-type]


# ----- the ceiling on the project ---------------------------------------------


class TestProjectCeiling:
    def test_absent_means_only_if_the_default_already_merges(self) -> None:
        """The only default that cannot silently widen a machine on upgrade."""
        assert not settings(merge_mode=REVIEW).automerge_allowed
        assert settings(merge_mode=AUTOMERGE).automerge_allowed

    def test_a_declared_switch_wins(self) -> None:
        assert settings(merge_mode=REVIEW, allow_automerge=True).automerge_allowed

    def test_review_is_always_offered(self) -> None:
        assert settings(merge_mode=REVIEW).offerable_merge_modes() == [REVIEW]

    def test_automerge_is_offered_where_allowed(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        assert raised.offerable_merge_modes() == [REVIEW, AUTOMERGE]


class TestCeilingParsing:
    def test_allow_automerge_is_read_from_the_file(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, merge_mode="review", allow_automerge=True)
        config = load_dispatch_config(home)
        assert config is not None
        assert config.projects["sandbox"].automerge_allowed

    @pytest.mark.parametrize(
        ("legacy", "allowed"),
        [("autonomous", True), ("auto", False), ("supervised", False), ("read_only", False)],
    )
    def test_a_legacy_max_posture_maps_onto_it(
        self, home: Path, fake_runner: Path, legacy: str, allowed: bool
    ) -> None:
        """a4: only ``autonomous`` ever let a run merge itself."""
        write_config(home, fake_runner, posture="auto", max_posture=legacy)
        config = load_dispatch_config(home)
        assert config is not None
        assert config.projects["sandbox"].automerge_allowed is allowed

    def test_an_unknown_legacy_ceiling_is_refused(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, max_posture="yolo")
        with pytest.raises(DispatchConfigError, match="max_posture"):
            load_dispatch_config(home)

    def test_an_unknown_mode_is_refused(self, home: Path, fake_runner: Path) -> None:
        write_config(home, fake_runner, merge_mode="yolo")
        with pytest.raises(DispatchConfigError, match="merge_mode"):
            load_dispatch_config(home)

    def test_an_automerge_default_on_a_project_that_forbids_it_is_refused(
        self, home: Path, fake_runner: Path
    ) -> None:
        """Refused rather than clamped: both halves were typed by the same person.

        Unlike the two sources the ceiling exists to bound, there is nobody here to
        protect from anybody, so quietly running their default as review would be the
        tool deciding which of two contradictory lines they meant.
        """
        write_config(home, fake_runner, merge_mode="automerge", allow_automerge=False)
        with pytest.raises(DispatchConfigError, match="allow_automerge"):
            load_dispatch_config(home)


# ----- precedence (sc-3) ------------------------------------------------------


class TestPrecedence:
    """Most-specific-wins: dispatch choice > epic > history > task record > project default."""

    def test_nothing_named_gives_the_project_default(self) -> None:
        resolved = resolve_merge_mode(settings(merge_mode=REVIEW))
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.PROJECT
        assert not resolved.clamped

    def test_the_task_record_beats_the_project_default(self) -> None:
        resolved = resolve_merge_mode(settings(merge_mode=AUTOMERGE), task=REVIEW)
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.TASK

    def test_a_dispatch_time_choice_beats_the_task_record(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, task=REVIEW, requested=AUTOMERGE)
        assert resolved.merge_mode is AUTOMERGE
        assert resolved.source is MergeModeSource.DISPATCH

    def test_a_dispatch_time_choice_may_be_narrower_than_the_task_record(self) -> None:
        """Most-specific-wins is not most-permissive-wins, and the difference matters."""
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, task=AUTOMERGE, requested=REVIEW)
        assert resolved.merge_mode is REVIEW

    def test_a_task_automerge_is_honoured_where_allowed(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, task=AUTOMERGE)
        assert resolved.merge_mode is AUTOMERGE
        assert not resolved.clamped


class TestAnInheritedModeSitsBetweenDispatchAndTheTaskRecord:
    """Where the epic's choice lands in the order, and why it is above the record (task-316).

    A field on the child's own record is narrower in scope, so "most specific wins" would
    seem to put it first; it does not, because an inherited mode is a *person's* choice
    made when they authorised the epic, and the record's field is a value any agent can
    write. Letting the field win would mean "I dispatched the epic automerge" quietly
    meant something different per child.
    """

    def test_an_inherited_mode_beats_the_project_default(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, inherited=AUTOMERGE)
        assert resolved.merge_mode is AUTOMERGE
        assert resolved.source is MergeModeSource.EPIC
        assert not resolved.clamped

    def test_an_inherited_mode_beats_the_task_record(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, task=REVIEW, inherited=AUTOMERGE)
        assert resolved.merge_mode is AUTOMERGE
        assert resolved.source is MergeModeSource.EPIC

    def test_a_dispatch_time_choice_beats_an_inherited_one(self) -> None:
        """What makes ``dispatch walk --merge-mode`` mean anything."""
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, inherited=AUTOMERGE, requested=REVIEW)
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.DISPATCH

    def test_an_inherited_mode_may_be_narrower_than_the_task_record(self) -> None:
        """Inheritance is not "take the wider of the two"; it is "the person decided"."""
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        resolved = resolve_merge_mode(raised, task=AUTOMERGE, inherited=REVIEW)
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.EPIC

    def test_an_inherited_automerge_the_project_forbids_is_refused_not_clamped(self) -> None:
        """It is a dispatch-time choice one generation up, so it is treated as one.

        Only reachable when somebody turns ``allow_automerge`` off while a walk is
        running: the parent's own dispatch checked this same switch.
        """
        with pytest.raises(AutomergeNotAllowedError) as caught:
            resolve_merge_mode(settings(merge_mode=REVIEW), inherited=AUTOMERGE)
        message = str(caught.value)
        assert "epic" in message, "the refusal must say the mode was inherited"
        assert "allow_automerge" in message, "the refusal must name where the switch is"

    def test_the_source_is_recorded_as_the_epic_rather_than_as_a_dispatch(self) -> None:
        """A child that merged unreviewed is traceable to the act that bought it."""
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        data = resolve_merge_mode(raised, inherited=AUTOMERGE).as_data()
        assert data == {"merge_mode_source": "epic", "allow_automerge": True}

    def test_it_describes_itself_for_a_human_reading_a_log(self) -> None:
        raised = settings(merge_mode=REVIEW, allow_automerge=True)
        assert (
            resolve_merge_mode(raised, inherited=AUTOMERGE).describe()
            == "merge mode automerge (from the epic)"
        )


class TestTheCeilingClampsAndRefuses:
    """The asymmetry: a task record is clamped, a dispatch-time request is refused."""

    def test_a_task_automerge_the_project_forbids_is_clamped(self) -> None:
        resolved = resolve_merge_mode(settings(merge_mode=REVIEW), task=AUTOMERGE)
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.TASK
        assert resolved.requested is AUTOMERGE
        assert resolved.clamped

    def test_a_history_automerge_is_clamped_once_the_switch_is_off(self) -> None:
        """A continuation keeps its grant, except against a revocation."""
        resolved = resolve_merge_mode(settings(merge_mode=REVIEW), history=AUTOMERGE)
        assert resolved.merge_mode is REVIEW
        assert resolved.source is MergeModeSource.HISTORY
        assert resolved.requested is AUTOMERGE

    def test_a_dispatch_time_request_the_project_forbids_is_refused(self) -> None:
        with pytest.raises(AutomergeNotAllowedError) as caught:
            resolve_merge_mode(settings(merge_mode=REVIEW), requested=AUTOMERGE)
        assert "allow_automerge" in str(caught.value), "the refusal must name the switch"

    def test_the_refusal_carries_its_own_reason_code(self) -> None:
        with pytest.raises(AutomergeNotAllowedError) as caught:
            resolve_merge_mode(settings(merge_mode=REVIEW), requested=AUTOMERGE)
        assert caught.value.reason == "automerge_not_allowed"

    def test_a_project_default_the_switch_forbids_is_clamped_in_code(self) -> None:
        """Unreachable through the parser, which refuses it -- this is the code path for
        a settings object built by hand, and it must fail closed rather than open."""
        incoherent = settings(merge_mode=AUTOMERGE, allow_automerge=False)
        resolved = resolve_merge_mode(incoherent)
        assert resolved.merge_mode is REVIEW
        assert resolved.requested is AUTOMERGE


class TestTheAccount:
    """What ``ResolvedMergeMode`` puts on a record."""

    def test_an_unclamped_resolution_records_no_request(self) -> None:
        data = resolve_merge_mode(settings(merge_mode=REVIEW)).as_data()
        assert data == {"merge_mode_source": "project", "allow_automerge": False}

    def test_a_clamped_resolution_records_what_asked(self) -> None:
        data = resolve_merge_mode(settings(merge_mode=REVIEW), task=AUTOMERGE).as_data()
        assert data == {
            "merge_mode_source": "task",
            "allow_automerge": False,
            "merge_mode_requested": "automerge",
        }

    def test_as_data_never_repeats_the_mode_itself(self) -> None:
        """It is already a top-level field on the entry; two copies could disagree."""
        assert "merge_mode" not in resolve_merge_mode(settings(merge_mode=REVIEW)).as_data()


# ----- the whole chain (sc-4, sc-5) -------------------------------------------


class TestAgentWrittenMergeModeCannotWiden:
    """sc-5. The one test in this file that is a security property.

    An agent writes ``automerge`` onto its own task record -- which it can do -- on a
    project whose machine-local config never allowed it. The run must come out at
    ``review``, and the *process* must be started with the narrow flags.
    """

    def test_the_run_gets_review_not_the_task_one(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, merge_mode="review")  # automerge not allowed
        manager.update_task(ready_task.id, actor="claude", merge_mode=AUTOMERGE.value)

        handle = start(manager, project, home, ready_task.id)

        meta = RunDirectory(handle.directory.path).read_meta()
        assert meta["merge_mode"] == "review"

    def test_the_process_was_never_started_with_bypass_permissions(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Asserted on the recorded argv, not on the resolver."""
        write_config(home, fake_runner, merge_mode="review")
        manager.update_task(ready_task.id, actor="claude", merge_mode=AUTOMERGE.value)

        start(manager, project, home, ready_task.id)

        argv = dispatch_entry(manager, ready_task.id)["argv"]
        assert "bypassPermissions" not in " ".join(argv)  # type: ignore[arg-type]

    def test_the_record_says_it_was_cut_down_and_by_what(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """A reader must not have to open a machine-local file to find out why the
        task's own request did not take effect."""
        write_config(home, fake_runner, merge_mode="review")
        manager.update_task(ready_task.id, actor="claude", merge_mode=AUTOMERGE.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "review"
        assert data["merge_mode_source"] == "task"
        assert data["allow_automerge"] is False
        assert data["merge_mode_requested"] == "automerge"

    def test_allowing_it_on_the_machine_is_what_lets_it_through(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The switch, and only the switch, is what was stopping it. Without this the
        tests above would pass on a build that ignored the task field entirely."""
        write_config(home, fake_runner, merge_mode="review", allow_automerge=True)
        manager.update_task(ready_task.id, actor="claude", merge_mode=AUTOMERGE.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "automerge"
        assert data["merge_mode_source"] == "task"
        assert "merge_mode_requested" not in data
        assert "bypassPermissions" in " ".join(data["argv"])  # type: ignore[arg-type]


class TestTheDispatchEntryNamesItsSource:
    """sc-4, for the two ordinary cases."""

    def test_an_untouched_task_records_the_project_as_the_source(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, merge_mode="review")

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "review"
        assert data["merge_mode_source"] == "project"
        assert "merge_mode_requested" not in data

    def test_a_dispatch_time_choice_records_itself(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, merge_mode="review", allow_automerge=True)

        start(manager, project, home, ready_task.id, merge_mode=AUTOMERGE)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "automerge"
        assert data["merge_mode_source"] == "dispatch"

    def test_a_task_asking_for_review_needs_no_switch_to_be_honoured(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """Asking for *less* is safe by construction."""
        write_config(home, fake_runner, merge_mode="automerge")
        manager.update_task(ready_task.id, actor="claude", merge_mode=REVIEW.value)

        start(manager, project, home, ready_task.id)

        data = dispatch_entry(manager, ready_task.id)
        assert data["merge_mode"] == "review"
        assert data["merge_mode_source"] == "task"


class TestTheDispatchRefusal:
    def test_a_forbidden_automerge_request_refuses_the_whole_dispatch(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        write_config(home, fake_runner, merge_mode="review")

        with pytest.raises(AutomergeNotAllowedError):
            start(manager, project, home, ready_task.id, merge_mode=AUTOMERGE)

    def test_a_refused_dispatch_writes_no_dispatch_entry(
        self, manager: TaskManager, project: Project, home: Path, fake_runner: Path, ready_task
    ) -> None:
        """The refusal happens above the run lock and above every write, so a task that
        was refused looks exactly as it did before somebody asked."""
        write_config(home, fake_runner, merge_mode="review")

        with pytest.raises(AutomergeNotAllowedError):
            start(manager, project, home, ready_task.id, merge_mode=AUTOMERGE)

        task = manager.get_task(ready_task.id)
        assert task is not None
        assert not [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]


class TestTheTaskFieldIsOptional:
    def test_a_task_without_a_merge_mode_serialises_without_the_key(
        self, manager: TaskManager, ready_task
    ) -> None:
        """``exclude_none`` keeps the field out of every record that never set it."""
        reloaded = manager.storage.load_task(ready_task.id)
        assert reloaded is not None
        stored = manager.storage.canonical_bytes(reloaded).decode("utf-8")
        assert "merge_mode:" not in stored
        assert "posture:" not in stored

    def test_setting_and_clearing_it_round_trips(self, manager: TaskManager, ready_task) -> None:
        manager.update_task(ready_task.id, merge_mode=AUTOMERGE.value)
        task = manager.get_task(ready_task.id)
        assert task is not None and task.merge_mode is AUTOMERGE

        manager.update_task(ready_task.id, merge_mode=None)
        task = manager.get_task(ready_task.id)
        assert task is not None and task.merge_mode is None
