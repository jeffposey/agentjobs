"""The dispatcher's commit of what it wrote, and why there is nothing left to commit.

Task-203 added it. Every dispatched run appended a ``dispatch_result`` after the session
had made its final commit and exited, nobody committed it, the shared clone was left
dirty after each run, and the person who noticed was always the human. Task-182 paid the
other half of the same bill: the clean-tree gate had to *excuse* the tasks directory,
because dispatch dirtied it itself at both ends of a run.

Both were consequences of a record being a file in the repository being dispatched.
Task-402 removed the cause, so the module those tests drove is two unconditional answers
now, and this is what is left to assert about it. What used to be here -- real git
repositories, ``git commit --only`` leaving a colleague's staged work alone, a hook that
refuses, a project outside any repository -- was all about the commit itself.

**The behaviour those tests existed for is still asserted, one layer up**, where it is a
statement about a run rather than about a helper:
``tests/test_dispatch_on_sqlite.py::TestTheRunEndsWithoutTouchingTheCheckout`` drives a
real dispatch and checks the checkout is untouched, and
``TestTheCleanTreeCheckCoversTheTasksDirectory`` beside it checks the coverage task-182
had to give up has come back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs.dispatch.record_commit import (
    CommitOutcome,
    commit_task_record,
    task_file_exclusions,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from support import task_store


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    return TaskManager(task_store(tmp_path / "tasks"))


@pytest.fixture()
def task_id(manager: TaskManager) -> str:
    created = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    return created.id


class TestThereIsNothingToCommit:
    """A record is a row, so the dispatcher's writes never reach a working tree."""

    def test_it_says_so_rather_than_looking_for_a_file(
        self, manager: TaskManager, task_id: str
    ) -> None:
        outcome = commit_task_record(manager, task_id)

        assert isinstance(outcome, CommitOutcome)
        assert outcome.committed is False
        assert outcome.path is None
        assert task_id in outcome.detail
        assert "nothing to commit" in outcome.detail

    def test_it_returns_rather_than_raising_for_an_id_that_does_not_exist(
        self, manager: TaskManager
    ) -> None:
        """The caller is finishing a run; an exception here would fail the dispatch."""
        outcome = commit_task_record(manager, "task-999-absent")

        assert outcome.committed is False
        assert "task-999-absent" in outcome.detail

    def test_the_subject_and_actor_a_caller_offers_change_nothing(
        self, manager: TaskManager, task_id: str
    ) -> None:
        # Both are still accepted, because five call sites pass them and a signature
        # change would be churn for no gain; neither can affect an answer that is
        # already "there is no file".
        outcome = commit_task_record(manager, task_id, subject="anything", actor="claude")

        assert outcome.committed is False


class TestNothingIsExcludedFromTheCleanTreeCheck:
    """The exclusion task-182 needed, and what its absence buys back."""

    def test_no_path_is_excused(self, manager: TaskManager) -> None:
        # Empty, always. The tasks directory was excused because dispatch wrote into it;
        # it does not, so a real change under `tasks/` is a dirty tree again -- which is
        # the coverage task-182 had to give up and task-311 set out to recover.
        assert task_file_exclusions(manager) == []
