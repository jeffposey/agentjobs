"""A child of an epic runs on the runner the epic was dispatched with (task-453).

On 2026-09-18 an epic dispatched on ``claude-fable-5-1`` through the single-member
``big-dawg`` group started its first child on ``claude-opus-5``, the project default.
The child's record said ``posture_source: epic``, so inheritance was wired for the
posture and the runner rode straight past it: the walk resolved the child's runner from
configuration at child-start time.

The tests here assert on what reached the process -- the ``--model`` in the child's
recorded argv -- and not on the presence of an envelope field. task-375 had passing
envelope tests while this path was broken; a field can be present and unread.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
import yaml

from agentjobs.dispatch.auth_recovery import model_from_argv
from agentjobs.dispatch.config import RecordedRunnerUnavailableError
from agentjobs.dispatch.epic import (
    ChildVerdict,
    WalkSettings,
    WalkStop,
    describe_settings,
    inherited_runner,
    resolve_epic_authorization,
    walk_epic,
)
from agentjobs.dispatch.guards import (
    ConflictingAuthorizationError,
    DispatchRequest,
    dispatch_task,
)
from agentjobs.dispatch.ledger import find_run
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    DispatchCandidateData,
    DispatchMode,
    DispatchPosture,
    DispatchSelectionData,
    DispatchTrigger,
    Lifecycle,
    LogEntryType,
)
from agentjobs.projects import Project
from support import task_store
from test_dispatch_epic import PROJECT_CONFIG, Dispatcher, make_child

BIG = "big"
"""The runner a person chose for the epic. Its argv carries ``--model big-model``."""

DEFAULT = "fake"
"""The project's configured runner. Its argv carries ``--model default-model``."""

GROUP = "big-dawg"
"""A single-member group, as the real one is: it refuses rather than substitutes."""


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(task_store(project.root / "tasks"))


def make_parent_on(
    manager: TaskManager,
    *,
    runner: str = DEFAULT,
    runner_source: Optional[str] = None,
    group: Optional[str] = None,
    selection_source: Optional[str] = None,
    dispatched: bool = True,
) -> str:
    """An active epic a human dispatched, with a dispatch entry naming its runner.

    ``runner_source`` is what the entry says when a runner was named outright or carried
    from history; ``group``/``selection_source`` is what it says when a group chose it.
    The defaults produce the entry a project-default dispatch writes: a runner and
    nothing about where it came from.
    """
    parent = manager.create_task(
        title="An epic",
        category="general",
        summary="Umbrella.",
        description="Several children.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.add_log_entry(
        parent.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Work this epic."
    )
    stored = manager.get_task(parent.id)
    assert stored is not None
    human_entry = stored.log[-1].id
    manager.claim_task(parent.id, agent="claude")
    if not dispatched:
        return parent.id
    selection = None
    if group is not None:
        selection = DispatchSelectionData(
            group=group,
            source=selection_source or "dispatch",
            candidates=[DispatchCandidateData(runner=runner, eligible=True, selected=True)],
        )
    manager.record_dispatch(
        parent.id,
        actor="Jeff Posey",
        run_id="run_parent",
        agent="claude",
        runner=runner,
        runner_source=runner_source,
        mode=DispatchMode.SESSION,
        posture=DispatchPosture.AUTONOMOUS,
        posture_source="dispatch",
        trigger=DispatchTrigger.MANUAL,
        caused_by=human_entry,
        argv=[runner, "--model", f"{runner}-model"],
        cwd=".",
        git_head="0000000",
        selection=selection,
    )
    return parent.id


@pytest.fixture
def home(tmp_path: Path) -> Path:
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


def write_config(home: Path, runner_script: Path, *, big_enabled: bool = True) -> Path:
    config = {
        "version": 1,
        "enabled": True,
        "runners": {
            DEFAULT: {
                "argv": [
                    sys.executable,
                    str(runner_script),
                    "--model",
                    "default-model",
                    "{prompt}",
                ],
                "actor": "claude",
            },
            BIG: {
                "argv": [sys.executable, str(runner_script), "--model", "big-model", "{prompt}"],
                "actor": "claude",
            },
        },
        "runner_groups": {
            GROUP: {
                "description": "One member, on purpose.",
                "members": [{"runner": BIG, "enabled": big_enabled}],
            }
        },
        "projects": {
            "sandbox": {
                "enabled": True,
                "runner": DEFAULT,
                "require_clean_tree": False,
                "posture": "auto",
                "max_posture": "autonomous",
            }
        },
    }
    path = home / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def configured(home: Path, project: Project, tmp_path: Path) -> Path:
    runner = tmp_path / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (project.root / ".gitignore").write_text(".agentjobs/\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=project.root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project.root, capture_output=True)
    return write_config(home, runner)


def start(
    manager: TaskManager,
    project: Project,
    home: Path,
    child_id: str,
    *,
    now: Optional[datetime] = None,
) -> Tuple[object, Dict[str, object]]:
    """Dispatch ``child_id`` on its epic's authorisation and return its dispatch entry.

    ``now`` is the dispatcher's clock, for a second attempt that would otherwise sit
    inside the per-task cooldown -- a refusal this suite is not about.
    """
    handle = dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=DispatchRequest(
            task_id=child_id,
            trigger=DispatchTrigger.CHILD,
            on_behalf_of_parent=True,
        ),
        home=home,
        api_base="http://127.0.0.1:8765",
        now=now,
    )
    supervisor = getattr(handle, "supervisor", None)
    if supervisor is not None:
        supervisor.join(timeout=30)
    task = manager.get_task(child_id)
    assert task is not None
    entries = [entry for entry in task.log if entry.type is LogEntryType.DISPATCH]
    assert entries, "no dispatch entry was written"
    return handle, dict(entries[-1].data)


# ----- the observation, reproduced ---------------------------------------------


class TestARealChildRunsOnTheEpicsRunner:
    """a1, a2 and a5: the model string in the child's argv, after a real dispatch."""

    def test_the_2026_09_18_observation(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """a5. Epic dispatched on a named runner; child argv must carry that model.

        Against the code before task-453 this fails with ``default-model``: the child
        resolved ``projects.sandbox.runner`` and the epic's choice was never read.
        """
        parent_id = make_parent_on(manager, runner=BIG, runner_source="dispatch_runner")
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = start(manager, project, home, child_id)
        assert model_from_argv(entry["argv"]) == "big-model"
        assert entry["runner"] == BIG
        assert entry["runner_source"] == "epic"

    def test_a_group_chosen_for_the_epic_reaches_the_child(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """a1. The parent's selection names the group; the child's names it too."""
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = start(manager, project, home, child_id)
        assert model_from_argv(entry["argv"]) == "big-model"
        assert entry["runner"] == BIG
        assert entry["selection"]["group"] == GROUP
        assert entry["selection"]["source"] == "epic"
        assert entry["runner_source"] == "epic"

    def test_a_childs_second_attempt_inherits_too(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """a2. The retry goes through the same read, so it cannot re-resolve either."""
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        child_id = make_child(manager, parent_id, "First")
        first_handle, first = start(manager, project, home, child_id)
        first_run = getattr(first_handle, "run_id")
        assert find_run(home, first_run).status not in (
            "running",
            "starting",
        ), "the first attempt should have settled before the second is tried"
        # The child is `active` after its first run; a walk retries it in place.
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        _handle, second = start(manager, project, home, child_id, now=later)
        assert second["run_id"] != first["run_id"]
        assert model_from_argv(second["argv"]) == "big-model"
        assert second["runner"] == BIG
        child = manager.get_task(child_id)
        assert child is not None
        authorising = [e for e in child.log if e.data.get("authorizes_dispatch")]
        assert [e.data["epic"]["attempt"] for e in authorising] == [1, 2]
        assert all(e.data["epic"]["runner"] == BIG for e in authorising)

    def test_an_epic_on_the_project_default_changes_nothing(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """The other half: a default that reaches the child on its own is not relabelled."""
        parent_id = make_parent_on(manager, runner=DEFAULT)
        child_id = make_child(manager, parent_id, "First")
        _handle, entry = start(manager, project, home, child_id)
        assert model_from_argv(entry["argv"]) == "default-model"
        assert entry["runner"] == DEFAULT
        assert entry.get("runner_source") is None
        assert entry.get("selection") is None

    def test_the_authorising_entry_on_the_child_names_the_runner(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """The child's own record says the model was the epic's, next to who chose it."""
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        child_id = make_child(manager, parent_id, "First")
        start(manager, project, home, child_id)
        child = manager.get_task(child_id)
        assert child is not None
        note = next(e for e in child.log if e.data.get("authorizes_dispatch"))
        assert note.data["epic"]["runner"] == BIG
        assert note.data["epic"]["group"] == GROUP
        assert f"runner `{BIG}`" in note.body
        assert f"group `{GROUP}`" in note.body

    def test_a_child_dispatch_that_also_names_a_runner_is_refused(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        """Two answers to 'which runner' is a conflict, not a precedence question."""
        parent_id = make_parent_on(manager, runner=BIG, runner_source="dispatch_runner")
        child_id = make_child(manager, parent_id, "First")
        with pytest.raises(ConflictingAuthorizationError):
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id,
                    trigger=DispatchTrigger.CHILD,
                    on_behalf_of_parent=True,
                    runner=DEFAULT,
                ),
                home=home,
                api_base="http://127.0.0.1:8765",
            )


# ----- a4: unavailable means refused, never substituted ---------------------------


class TestAnUnavailableEpicRunnerRefusesRatherThanFallsBack:
    def test_the_dispatcher_refuses_by_name(
        self, manager: TaskManager, project: Project, home: Path, configured: Path, tmp_path: Path
    ) -> None:
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        child_id = make_child(manager, parent_id, "First")
        write_config(home, tmp_path / "runner.py", big_enabled=False)
        with pytest.raises(RecordedRunnerUnavailableError) as caught:
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id,
                    trigger=DispatchTrigger.CHILD,
                    on_behalf_of_parent=True,
                ),
                home=home,
                api_base="http://127.0.0.1:8765",
            )
        assert caught.value.reason == "recorded_runner_unavailable"
        assert "epic" in str(caught.value)
        assert BIG in str(caught.value) and GROUP in str(caught.value)
        assert DEFAULT not in str(caught.value)
        child = manager.get_task(child_id)
        assert child is not None
        assert (
            child.lifecycle is Lifecycle.READY
        ), "nothing was claimed for a run that never started"
        assert not [e for e in child.log if e.type is LogEntryType.DISPATCH]

    def test_a_runner_removed_from_the_file_is_refused_too(
        self, manager: TaskManager, project: Project, home: Path, configured: Path
    ) -> None:
        parent_id = make_parent_on(manager, runner="retired", runner_source="dispatch_runner")
        child_id = make_child(manager, parent_id, "First")
        with pytest.raises(RecordedRunnerUnavailableError) as caught:
            dispatch_task(
                manager=manager,
                project=project,
                project_config=PROJECT_CONFIG,
                request=DispatchRequest(
                    task_id=child_id,
                    trigger=DispatchTrigger.CHILD,
                    on_behalf_of_parent=True,
                ),
                home=home,
                api_base="http://127.0.0.1:8765",
            )
        assert "undefined_runner" in str(caught.value)

    def test_the_walk_stops_with_the_reason_rather_than_crashing(
        self, manager: TaskManager, project: Project, tmp_path: Path
    ) -> None:
        """The refusal is a configuration error, not a `DispatchRefused`; the walk
        used to let that family escape uncaught and end the supervisor with nothing on
        the record. Now it grounds, and the detail names the reason."""
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        first = make_child(manager, parent_id, "First")
        second = make_child(manager, parent_id, "Second")

        class Refusing(Dispatcher):
            def __call__(self, *, request, **kwargs):  # type: ignore[override]
                self.started.append(request.task_id)
                raise RecordedRunnerUnavailableError(
                    f"This child inherits from an epic dispatched on runner {BIG!r} in "
                    f"group {GROUP!r}, and that runner cannot run here now (big (disabled))."
                )

        dispatcher = Refusing(manager)
        events: List[str] = []
        result = walk_epic(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            parent_id=parent_id,
            home=tmp_path / "machine",
            durable=False,
            settings=WalkSettings(poll_seconds=0.0, child_timeout_seconds=10.0),
            dispatch=dispatcher,
            read_run_status=lambda _run: None,
            sleep=lambda _s: None,
            now=lambda: 0.0,
            on_event=events.append,
        )
        assert result.stop is WalkStop.COULD_NOT_START_CHILD
        assert "recorded_runner_unavailable" in result.detail
        assert BIG in result.detail
        assert [a.verdict for a in result.attempts] == [ChildVerdict.DIED]
        assert dispatcher.started == [first], "nothing further took off"
        untouched = manager.get_task(second)
        assert untouched is not None and untouched.lifecycle is Lifecycle.READY


# ----- which sources cross the boundary --------------------------------------------


class TestWhichRunnerSourcesCrossTheBoundary:
    def test_a_runner_named_for_the_dispatch_crosses(self, manager: TaskManager) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=BIG, runner_source="dispatch_runner")
        )
        assert parent is not None
        assert inherited_runner(parent) == (BIG, None)

    def test_a_group_named_for_the_dispatch_crosses_with_its_member(
        self, manager: TaskManager
    ) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        )
        assert parent is not None
        assert inherited_runner(parent) == (BIG, GROUP)

    def test_an_epic_of_an_epic_passes_the_choice_on(self, manager: TaskManager) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=BIG, group=GROUP, selection_source="epic")
        )
        assert parent is not None
        assert inherited_runner(parent) == (BIG, GROUP)

    def test_a_resumed_parent_passes_its_frozen_runner_on(self, manager: TaskManager) -> None:
        """A continuation's runner is what it was granted; a walk from it inherits that."""
        parent = manager.get_task(
            make_parent_on(
                manager,
                runner=BIG,
                runner_source="history",
                group=GROUP,
                selection_source="history",
            )
        )
        assert parent is not None
        assert inherited_runner(parent) == (BIG, GROUP)

    def test_a_project_group_does_not_cross(self, manager: TaskManager) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=DEFAULT, group="default", selection_source="project")
        )
        assert parent is not None
        assert inherited_runner(parent) is None

    def test_a_project_runner_does_not_cross(self, manager: TaskManager) -> None:
        parent = manager.get_task(make_parent_on(manager, runner=DEFAULT))
        assert parent is not None
        assert inherited_runner(parent) is None

    def test_an_undispatched_parent_passes_nothing(self, manager: TaskManager) -> None:
        parent = manager.get_task(make_parent_on(manager, dispatched=False))
        assert parent is not None
        assert inherited_runner(parent) is None

    def test_the_authorisation_carries_it(self, manager: TaskManager) -> None:
        parent_id = make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        child = manager.get_task(make_child(manager, parent_id, "First"))
        assert child is not None
        authorization = resolve_epic_authorization(manager, PROJECT_CONFIG, child)
        assert authorization.runner == BIG
        assert authorization.group == GROUP
        assert authorization.data()["epic"]["runner"] == BIG


# ----- a3: the walk says which runner before it starts -----------------------------


class TestTheWalkSaysWhichRunnerChildrenStartOn:
    def test_an_inherited_runner_is_named_beside_the_posture(self, manager: TaskManager) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=BIG, group=GROUP, selection_source="dispatch")
        )
        assert parent is not None
        lines = describe_settings(WalkSettings(), inherited_runner=inherited_runner(parent))
        runner_line = next(line for line in lines if line.startswith("runner children start on:"))
        assert f"{BIG} from group {GROUP}" in runner_line
        assert "inherited from the epic's own dispatch" in runner_line
        assert "refused rather than started elsewhere" in runner_line

    def test_a_runner_without_a_group_is_named_alone(self, manager: TaskManager) -> None:
        parent = manager.get_task(
            make_parent_on(manager, runner=BIG, runner_source="dispatch_runner")
        )
        assert parent is not None
        lines = describe_settings(WalkSettings(), inherited_runner=inherited_runner(parent))
        runner_line = next(line for line in lines if line.startswith("runner children start on:"))
        assert f"start on: {BIG} (" in runner_line
        assert "group" not in runner_line

    def test_no_inheritance_says_so_rather_than_saying_nothing(self) -> None:
        lines = describe_settings(WalkSettings())
        runner_line = next(line for line in lines if line.startswith("runner children start on:"))
        assert "resolved as each child starts" in runner_line
