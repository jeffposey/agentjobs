"""A retry or resume keeps what its execution was granted (task-375).

Driven through ``dispatch_task`` against a real journal, a real task store and a real
batch process, because each defect this closes was invisible to a unit of the resolver:
task-410's resume came back on a different model with every resolver behaving exactly as
written. The fixtures are the two shapes on the record:

- **task-410's big-dawg continuation.** A person dispatches a task through the
  ``big-dawg`` group; later the machine continues it on their handback. The continuation
  must run the same runner at the same posture, whatever the defaults say by then, and
  refuse by name when that runner can no longer run.
- **Revocation.** Frozen is not irrevocable: the kill switch, a lowered ceiling and a Stop
  all defeat the old grant, and a raised ceiling does not widen it.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from agentjobs.dispatch.config import (
    DispatchSentinelError,
    MergeMode,
    RecordedRunnerUnavailableError,
    sentinel_path,
)
from agentjobs.dispatch.envelope import GrantStoppedError, is_continuation
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
from agentjobs.dispatch.ledger import DispatchLedger
from agentjobs.dispatch.phases import RUN_ID_ENV
from agentjobs.execution.factory import execution_store_for
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger, LogEntryType, utcnow
from agentjobs.projects import Project
from test_dispatch_guards import PROJECT_CONFIG, home, manager, project, ready_task, settle

__all__ = ["home", "manager", "project", "ready_task"]  # fixtures, imported by name

SECRET = "s3cr3t-token-value"


@pytest.fixture
def runner_script(tmp_path: Path) -> Path:
    """A batch runner that writes down the credential it was handed, then exits."""
    script = tmp_path / "runner.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "out.mkdir(exist_ok=True)\n"
        f"run = os.environ.get({RUN_ID_ENV!r}, 'unknown')\n"
        "(out / run).write_text(os.environ.get('AGENTJOBS_RUN_CREDENTIAL', ''))\n",
        encoding="utf-8",
    )
    return script


def write_config(
    home: Path,
    script: Path,
    *,
    fable_enabled: bool = True,
    fable_defined: bool = True,
    fable_executable: Optional[str] = None,
    project_group: str = "default",
    merge_mode: str = "review",
    allow_automerge: bool = True,
    extra_runners: Optional[Dict[str, object]] = None,
) -> None:
    """Two groups, the way this machine has them: ``default`` and ``big-dawg``."""
    credentials = str(script.parent / "credentials")
    runners: Dict[str, object] = {
        "opus": {"argv": [sys.executable, str(script), credentials, "{prompt}"], "actor": "claude"},
    }
    if fable_defined:
        runners["fable"] = {
            "argv": [fable_executable or sys.executable, str(script), credentials, "{prompt}"],
            "actor": "claude",
            "env": {"FABLE_TOKEN": SECRET},
        }
    runners.update(extra_runners or {})
    config = {
        "version": 1,
        "enabled": True,
        "runners": runners,
        "runner_groups": {
            "default": {"members": ["opus"]},
            "big-dawg": {
                "members": [
                    {"runner": "fable", "enabled": fable_enabled, "note": "kept for hard ones"},
                    "opus",
                ]
            },
        },
        "projects": {
            "sandbox": {
                "enabled": True,
                "group": project_group,
                "require_clean_tree": False,
                "merge_mode": merge_mode,
                "allow_automerge": allow_automerge,
            }
        },
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def dispatch(
    manager: TaskManager,
    project: Project,
    home: Path,
    request: DispatchRequest,
    *,
    minutes_later: int = 0,
):
    handle = dispatch_task(
        manager=manager,
        project=project,
        project_config=PROJECT_CONFIG,
        request=request,
        home=home,
        api_base="http://127.0.0.1:8765",
        now=utcnow() + timedelta(minutes=minutes_later),
    )
    settle(handle)
    return handle


def human_note(manager: TaskManager, task_id: str, body: str = "One more change.") -> int:
    task = manager.add_log_entry(task_id, actor="Jeff Posey", type=LogEntryType.NOTE, body=body)
    return task.log[-1].id


def continuation(task_id: str, caused_by: int) -> DispatchRequest:
    """What ``deliver_handback`` asks for: the machine, on a person's entry, choosing nothing."""
    return DispatchRequest(task_id=task_id, trigger=DispatchTrigger.AUTO, caused_by=caused_by)


def dispatch_entries(manager: TaskManager, task_id: str):
    task = manager.get_task(task_id)
    assert task is not None
    return [dict(e.data or {}) for e in task.log if e.type is LogEntryType.DISPATCH]


def big_dawg_grant(
    manager, project, home, task_id: str, merge_mode: MergeMode = MergeMode.AUTOMERGE
):
    return dispatch(
        manager,
        project,
        home,
        DispatchRequest(task_id=task_id, group="big-dawg", merge_mode=merge_mode),
    )


class TestWhatCountsAsAContinuation:
    def test_the_machine_on_a_handback_choosing_nothing_is_one(self) -> None:
        assert is_continuation(continuation("task-001", 3))

    @pytest.mark.parametrize(
        "change",
        [
            {"trigger": DispatchTrigger.MANUAL},
            {"trigger": DispatchTrigger.CHILD, "on_behalf_of_parent": True},
            {"runner": "opus"},
            {"group": "default"},
            {"merge_mode": MergeMode.REVIEW},
            {"authorized_by": "Jeff Posey"},
        ],
    )
    def test_anything_a_person_chose_is_a_new_grant(self, change: Dict[str, object]) -> None:
        fields: Dict[str, object] = {"task_id": "task-001", "trigger": DispatchTrigger.AUTO}
        fields.update(change)
        assert not is_continuation(DispatchRequest(**fields))  # type: ignore[arg-type]


class TestTheBigDawgContinuation:
    """durable-1: task-410 came back on ``claude-opus-5`` after going out on fable."""

    def test_the_continuation_keeps_runner_group_and_posture_after_defaults_change(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        first = big_dawg_grant(manager, project, home, ready_task.id)
        # The defaults move underneath it: a new project group and a narrower default.
        write_config(home, runner_script, project_group="default", merge_mode="review")

        second = dispatch(
            manager,
            project,
            home,
            continuation(ready_task.id, human_note(manager, ready_task.id)),
            minutes_later=2,
        )

        entry = dispatch_entries(manager, ready_task.id)[-1]
        assert entry["run_id"] == second.run_id
        assert entry["runner"] == "fable"
        assert entry["runner_source"] == "history"
        assert entry["selection"]["group"] == "big-dawg"
        assert entry["selection"]["source"] == "history"
        assert entry["merge_mode"] == "automerge"
        assert entry["merge_mode_source"] == "history"
        store = execution_store_for(home)
        first_execution = store.attempt(first.run_id).execution_id  # type: ignore[union-attr]
        assert entry["envelope"] == {
            "source": "history",
            "execution_id": store.attempt(second.run_id).execution_id,  # type: ignore[union-attr]
            "continues_execution_id": first_execution,
        }
        assert "releases the merge gate" in entry["argv"][-1]

    def test_a_continuation_of_a_continuation_still_keeps_the_first_grant(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        first = big_dawg_grant(manager, project, home, ready_task.id)
        write_config(home, runner_script, merge_mode="review")
        for minutes in (2, 4):
            dispatch(
                manager,
                project,
                home,
                continuation(ready_task.id, human_note(manager, ready_task.id)),
                minutes_later=minutes,
            )

        entries = dispatch_entries(manager, ready_task.id)
        assert [e["runner"] for e in entries] == ["fable", "fable", "fable"]
        store = execution_store_for(home)
        newest = store.latest_execution("sandbox", ready_task.id)
        assert newest is not None
        root = store.attempt(first.run_id).execution_id  # type: ignore[union-attr]
        assert newest.envelope["grant"]["root_execution_id"] == root

    @pytest.mark.parametrize(
        "revoked",
        [
            {"fable_enabled": False},
            {"fable_defined": False},
            {"fable_executable": "definitely-not-installed-anywhere"},
        ],
        ids=["disabled-in-its-group", "removed-from-runners", "executable-gone"],
    )
    def test_an_unavailable_recorded_runner_refuses_rather_than_falling_back(
        self,
        manager,
        project,
        home: Path,
        runner_script: Path,
        ready_task,
        revoked: Dict[str, object],
    ) -> None:
        write_config(home, runner_script)
        big_dawg_grant(manager, project, home, ready_task.id)
        write_config(home, runner_script, **revoked)  # type: ignore[arg-type]

        with pytest.raises(RecordedRunnerUnavailableError) as caught:
            dispatch(
                manager,
                project,
                home,
                continuation(ready_task.id, human_note(manager, ready_task.id)),
                minutes_later=2,
            )

        assert "fable" in str(caught.value)
        # `opus` could run, and is in the same group. Nothing started on it.
        assert [e["runner"] for e in dispatch_entries(manager, ready_task.id)] == ["fable"]

    def test_a_person_dispatching_again_is_a_new_grant_from_todays_defaults(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        big_dawg_grant(manager, project, home, ready_task.id)
        human_note(manager, ready_task.id, "Run it again.")

        dispatch(manager, project, home, DispatchRequest(task_id=ready_task.id), minutes_later=2)

        entry = dispatch_entries(manager, ready_task.id)[-1]
        assert entry["runner"] == "opus"
        assert entry["merge_mode"] == "review"
        assert entry["envelope"]["source"] == "grant"


class TestRevocationWins:
    """durable-3: frozen inputs are not irrevocable authority."""

    def test_the_kill_switch_refuses_a_continuation(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        big_dawg_grant(manager, project, home, ready_task.id)
        sentinel_path(home).write_text("", encoding="utf-8")

        with pytest.raises(DispatchSentinelError):
            dispatch(
                manager,
                project,
                home,
                continuation(ready_task.id, human_note(manager, ready_task.id)),
                minutes_later=2,
            )

    def test_a_lowered_ceiling_narrows_the_historical_grant(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        big_dawg_grant(manager, project, home, ready_task.id)
        write_config(home, runner_script, allow_automerge=False)

        dispatch(
            manager,
            project,
            home,
            continuation(ready_task.id, human_note(manager, ready_task.id)),
            minutes_later=2,
        )

        entry = dispatch_entries(manager, ready_task.id)[-1]
        assert entry["merge_mode"] == "review"
        assert entry["merge_mode_source"] == "history"
        assert entry["merge_mode_requested"] == "automerge"
        assert "Do not merge." in entry["argv"][-1]

    def test_a_raised_ceiling_does_not_widen_the_historical_grant(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script, allow_automerge=False)
        big_dawg_grant(manager, project, home, ready_task.id, merge_mode=MergeMode.REVIEW)
        write_config(home, runner_script, merge_mode="automerge", allow_automerge=True)

        dispatch(
            manager,
            project,
            home,
            continuation(ready_task.id, human_note(manager, ready_task.id)),
            minutes_later=2,
        )

        entry = dispatch_entries(manager, ready_task.id)[-1]
        assert entry["merge_mode"] == "review"
        assert entry["merge_mode_source"] == "history"
        assert "merge_mode_requested" not in entry

    def test_a_stopped_execution_is_not_continued_automatically(
        self, manager, project, home: Path, runner_script: Path, ready_task, tmp_path: Path
    ) -> None:
        slow = tmp_path / "slow.py"
        slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        write_config(
            home,
            runner_script,
            extra_runners={
                "fable": {"argv": [sys.executable, str(slow), "{prompt}"], "actor": "claude"}
            },
        )
        handle = dispatch_task(
            manager=manager,
            project=project,
            project_config=PROJECT_CONFIG,
            request=DispatchRequest(task_id=ready_task.id, group="big-dawg"),
            home=home,
            api_base="http://127.0.0.1:8765",
        )
        result = DispatchLedger(home, managers={"sandbox": manager}).cancel(
            handle.run_id, actor="Jeff Posey", source="cli"
        )
        assert result.stopped, result.detail
        settle(handle)

        with pytest.raises(GrantStoppedError) as caught:
            dispatch(
                manager,
                project,
                home,
                continuation(ready_task.id, human_note(manager, ready_task.id)),
                minutes_later=2,
            )
        assert "Dispatch the task again" in str(caught.value)

        # A person dispatching again is a new grant, and is not refused.
        human_note(manager, ready_task.id, "Start it again.")
        dispatch(manager, project, home, DispatchRequest(task_id=ready_task.id), minutes_later=4)
        assert dispatch_entries(manager, ready_task.id)[-1]["envelope"]["source"] == "grant"


class TestTheEnvelopeHoldsNoSecret:
    """durable-4."""

    def test_the_journal_records_the_env_names_and_never_their_values(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        handle = big_dawg_grant(manager, project, home, ready_task.id)

        store = execution_store_for(home)
        execution = store.execution(store.attempt(handle.run_id).execution_id)  # type: ignore[arg-type, union-attr]
        assert execution is not None
        envelope = execution.envelope
        assert envelope["env_keys"] == ["FABLE_TOKEN"]
        assert envelope["runner"] == "fable"
        assert envelope["merge_mode"] == "automerge"
        assert envelope["push"] is False
        assert "releases the merge gate" in envelope["policy_clause"]
        assert envelope["envelope_version"] == 1
        assert SECRET not in json.dumps(envelope)
        history = json.dumps([event.payload for event in store.events(execution.execution_id)])
        assert SECRET not in history

    def test_a_continuation_is_handed_its_own_credential_not_the_last_runs(
        self, manager, project, home: Path, runner_script: Path, ready_task
    ) -> None:
        write_config(home, runner_script)
        first = big_dawg_grant(manager, project, home, ready_task.id)
        second = dispatch(
            manager,
            project,
            home,
            continuation(ready_task.id, human_note(manager, ready_task.id)),
            minutes_later=2,
        )

        issued = runner_script.parent / "credentials"
        first_token = (issued / first.run_id).read_text(encoding="utf-8")
        second_token = (issued / second.run_id).read_text(encoding="utf-8")
        assert first_token.startswith(f"{first.run_id}.")
        assert second_token.startswith(f"{second.run_id}.")
        assert first_token != second_token


class TestHistoryInThePostureOrder:
    """Where ``history`` sits among the sources ``resolve_posture`` weighs."""

    @staticmethod
    def settings(merge_mode: MergeMode = MergeMode.REVIEW, allow_automerge: bool = True):
        from agentjobs.dispatch.config import ProjectDispatchSettings

        return ProjectDispatchSettings(
            project_id="sandbox",
            enabled=True,
            merge_mode=merge_mode,
            allow_automerge=allow_automerge,
        )

    def test_history_outranks_the_task_field_and_the_project_default(self) -> None:
        from agentjobs.dispatch.config import MergeModeSource, resolve_merge_mode

        resolved = resolve_merge_mode(
            self.settings(MergeMode.REVIEW),
            task=MergeMode.REVIEW,
            history=MergeMode.AUTOMERGE,
        )
        assert resolved.merge_mode is MergeMode.AUTOMERGE
        assert resolved.source is MergeModeSource.HISTORY

    def test_a_choice_made_now_outranks_history(self) -> None:
        from agentjobs.dispatch.config import MergeModeSource, resolve_merge_mode

        resolved = resolve_merge_mode(
            self.settings(), requested=MergeMode.REVIEW, history=MergeMode.AUTOMERGE
        )
        assert resolved.source is MergeModeSource.DISPATCH

    def test_history_above_the_ceiling_is_clamped_not_refused(self) -> None:
        from agentjobs.dispatch.config import resolve_merge_mode

        resolved = resolve_merge_mode(
            self.settings(allow_automerge=False), history=MergeMode.AUTOMERGE
        )
        assert resolved.merge_mode is MergeMode.REVIEW
        assert resolved.requested is MergeMode.AUTOMERGE
