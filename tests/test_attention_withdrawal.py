"""A demand for attention is withdrawn when its reason is resolved (task-467).

Two clauses, and the second one is what these cover. A task may demand a person's
attention only when there is something for them to do on that task *now* -- and that
demand has to be taken back when it stops being true, by something that does not depend
on the person noticing.

The incident, from task-421 on 2026-09-19. A child parked for review, the epic walk
handed the **parent** to ``human``/``decision`` as well, and the walk then stopped. The
person approved the child; it gated, merged and closed twenty minutes later. Nothing
retracted the parent's ball. It became a permanent member of the attention waiting set,
and because an episode owes exactly one interruptive alert and only resets when that set
*empties*, one stuck row silenced the alarm for every wait that came after it. The
defect disabled the feature the epic existed to deliver, which is why the episode
assertions below are the point rather than a flourish.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

import pytest

from agentjobs.attention import reconcile
from agentjobs.dashboard import (
    build_dashboard_snapshot,
    count_blocking_human,
    deferred_to_child,
    human_waiting_tasks,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    LabelledTask,
    Lifecycle,
    LogEntryType,
    Outcome,
    Task,
)
from agentjobs.retraction import Finding, retract, survey, waiting_on_stamp
from support import task_store


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    return TaskManager(task_store(tmp_path / "tasks"))


def epic(manager: TaskManager, *, title: str = "Epic") -> Task:
    """A parent someone is supervising."""
    parent = manager.create_task(
        title=title,
        category="general",
        summary="An epic.",
        description="Walk it.",
        lifecycle=Lifecycle.READY,
    )
    return manager.claim_task(parent.id, agent="claude")


def child(manager: TaskManager, parent: Task, title: str) -> Task:
    created = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=f"Do {title}.",
        lifecycle=Lifecycle.READY,
        parent=parent.id,
    )
    return manager.claim_task(created.id, agent="claude")


def park(manager: TaskManager, task: Task) -> Task:
    """Hand a task to a person for review, which is the one real ask in all of this."""
    return manager.handoff(
        task.id,
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="The branch is ready. Read it and approve or object.",
    )


def ask_about(manager: TaskManager, parent: Task, child_id: str, *, stamped: bool) -> Task:
    """The handoff the walk used to write: the parent, asked about a child.

    Both shapes, because the sweep has to read the corpus that already exists as well as
    the one written from now on. ``stamped`` is the structured ``waiting_on`` a walk
    attaches today; without it the only trace is the id in the prose, which is what the
    parents written before task-467 have.
    """
    return manager.handoff(
        parent.id,
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        ball_prompt=f"The epic walk stopped on {child_id}. Read that child and decide.",
        data={"waiting_on": child_id} if stamped else None,
    )


def ids(tasks: Sequence[LabelledTask]) -> List[str]:
    return [task.id for task in tasks]


class TestOneClickIsOneAsk:
    """A parent whose child holds the ball is not a second thing to do."""

    def test_a_parent_waiting_on_a_parked_child_is_not_in_the_waiting_set(
        self, manager: TaskManager
    ) -> None:
        parent = epic(manager)
        only = child(manager, parent, "First")
        park(manager, only)
        ask_about(manager, parent, only.id, stamped=True)

        waiting = human_waiting_tasks(manager)

        assert ids(waiting) == [only.id], "the click is on the child; the parent is not a gate"
        assert count_blocking_human(manager) == 1

    def test_the_dashboard_panel_and_the_badge_agree(self, manager: TaskManager) -> None:
        """One predicate, one order -- the finding behind ``test_attention_tiers``."""
        parent = epic(manager)
        only = child(manager, parent, "First")
        park(manager, only)
        ask_about(manager, parent, only.id, stamped=True)

        snapshot = build_dashboard_snapshot(manager)

        assert ids(snapshot["waiting_tasks"]) == ids(human_waiting_tasks(manager))
        assert snapshot["stats"]["waiting_for_human"] == count_blocking_human(manager) == 1

    def test_an_unspecced_draft_is_backlog_and_not_a_blockage(self, manager: TaskManager) -> None:
        """Regression for the term that used to dominate the list: 29 of 30 rows.

        A draft parked on ``human``/``spec`` is an unfiled idea with no deadline. It is
        counted by ``awaits_human_input`` and by nothing else, and the sweep above has to
        leave that true or fixing the epic case changes nothing anybody notices.
        """
        for index in range(3):
            manager.create_task(
                title=f"Idea {index}",
                category="general",
                summary="An idea.",
                description="Not specified yet.",
            )

        assert count_blocking_human(manager) == 0
        assert build_dashboard_snapshot(manager)["stats"]["awaiting_input"] == 3

    def test_a_parent_whose_child_closed_is_still_counted(self, manager: TaskManager) -> None:
        """The predicate is narrow on purpose, which is why the sweep has to exist.

        Hiding every parent with an open child would hide real asks. So a parent whose
        child has *closed* is still in the set -- and that is exactly the row task-421
        was stuck as. Nothing here fixes it; :class:`TestTheSweepTakesItBack` does.
        """
        parent = epic(manager)
        only = child(manager, parent, "First")
        park(manager, only)
        ask_about(manager, parent, only.id, stamped=True)
        manager.close_task(only.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        assert ids(human_waiting_tasks(manager)) == [parent.id]

    def test_the_predicate_itself_names_the_child(self, manager: TaskManager) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        second = park(manager, child(manager, parent, "Second"))

        assert deferred_to_child(parent, [first, second]) is second
        assert deferred_to_child(parent, [first]) is None


class TestTheSweepTakesItBack:
    """The second clause: nothing depends on the person noticing."""

    def sweep(self, manager: TaskManager) -> List[str]:
        return retract(
            manager.list_tasks(),
            manager.get_subtasks,
            handoff=manager.handoff,
            log=manager.add_log_entry,
        )

    @pytest.mark.parametrize("stamped", [True, False], ids=["stamped", "named-in-prose"])
    def test_a_resolved_child_takes_its_parents_ask_with_it(
        self, manager: TaskManager, stamped: bool
    ) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        child(manager, parent, "Second")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=stamped)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        lines = self.sweep(manager)

        assert any(parent.id in line for line in lines), lines
        assert human_waiting_tasks(manager) == []
        corrected = manager.get_task(parent.id)
        assert corrected is not None and corrected.ball is not Ball.HUMAN

    def test_it_says_what_it_found_before_it_changes_anything(self, manager: TaskManager) -> None:
        """``survey`` is pure, so a dry run predicts the sweep rather than guessing at it."""
        parent = epic(manager)
        first = child(manager, parent, "First")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        findings = survey(manager.list_tasks(), manager.get_subtasks)

        assert [finding.task_id for finding in findings] == [parent.id]
        assert findings[0].resolved == [first.id]
        assert first.id in findings[0].describe()
        still = manager.get_task(parent.id)
        assert still is not None and still.ball is Ball.HUMAN, "the survey wrote nothing"

    def test_a_parent_with_another_live_child_waits_on_that_child(
        self, manager: TaskManager
    ) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        second = child(manager, parent, "Second")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        self.sweep(manager)

        corrected = manager.get_task(parent.id)
        assert corrected is not None
        assert corrected.ball is Ball.EXTERNAL and corrected.ball_reason is BallReason.DEPENDENCY
        assert second.id in (corrected.ball_prompt or "")
        assert waiting_on_stamp(corrected) == second.id, "the correction stamps its own wait"

    def test_a_parent_whose_children_have_all_closed_is_left_with_the_person(
        self, manager: TaskManager
    ) -> None:
        """The remaining act is real: judging the parent's own acceptance criteria."""
        parent = epic(manager)
        first = child(manager, parent, "First")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)
        # No other child; the walk's judging half is now genuinely somebody's.
        lines = self.sweep(manager)

        assert any("left with a person" in line for line in lines), lines
        kept = manager.get_task(parent.id)
        assert kept is not None and kept.ball is Ball.HUMAN

    def test_a_child_still_parked_is_not_touched(self, manager: TaskManager) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)

        assert self.sweep(manager) == []
        held = manager.get_task(parent.id)
        assert held is not None and held.ball is Ball.HUMAN

    def test_an_ask_that_names_no_child_is_never_touched(self, manager: TaskManager) -> None:
        """The conservative half. A person's question is not answered by a child closing."""
        parent = epic(manager)
        first = child(manager, parent, "First")
        manager.handoff(
            parent.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.DECISION,
            ball_prompt="Which of the two designs should this use?",
        )
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        assert self.sweep(manager) == []
        held = manager.get_task(parent.id)
        assert held is not None and held.ball is Ball.HUMAN

    def test_a_second_sweep_finds_nothing(self, manager: TaskManager) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        child(manager, parent, "Second")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        assert self.sweep(manager)
        assert self.sweep(manager) == [], "a sweep that keeps writing is a sweep that floods"

    def test_a_record_that_cannot_be_written_does_not_end_the_sweep(
        self, manager: TaskManager
    ) -> None:
        parent = epic(manager)
        first = child(manager, parent, "First")
        child(manager, parent, "Second")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected: the store said no")

        lines = retract(
            manager.list_tasks(),
            manager.get_subtasks,
            handoff=refuse,
            log=manager.add_log_entry,
        )

        assert any("could not be corrected" in line for line in lines), lines


class TestTheEpisodeIsNotHeldOpen:
    """The consequence that made this critical rather than untidy.

    An episode owes exactly one interruptive alert and resets only when the waiting set
    empties. A permanent member therefore does not merely add a row -- it stops the next
    genuinely new wait from ever interrupting.
    """

    def test_a_stale_parent_keeps_the_episode_open_until_the_sweep_runs(
        self, manager: TaskManager, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        parent = epic(manager)
        first = child(manager, parent, "First")
        later = child(manager, parent, "Second")
        park(manager, first)
        ask_about(manager, parent, first.id, stamped=True)

        opened = reconcile(manager, "inbox", home=home)
        assert opened.episode is not None
        first_episode = opened.episode.id

        # The person acts on it: approve, gate, merge, close.
        reconcile(manager, "inbox", home=home)
        manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)
        stuck = reconcile(manager, "inbox", home=home)
        assert stuck.blocking == 1, "the parent is the stale row the incident left behind"
        assert stuck.episode is not None and stuck.episode.id == first_episode

        retract(
            manager.list_tasks(),
            manager.get_subtasks,
            handoff=manager.handoff,
            log=manager.add_log_entry,
        )
        cleared = reconcile(manager, "inbox", home=home)

        assert cleared.blocking == 0 and cleared.episode is None, "the set never emptied"

        # And the next genuine wait gets its own episode, which is what the stale row
        # was suppressing.
        park(manager, manager.get_task(later.id) or later)
        fresh = reconcile(manager, "inbox", home=home)

        assert fresh.episode is not None and fresh.episode.id != first_episode
        assert fresh.alerting, "a new wait after a stale one must still be able to interrupt"


def test_a_finding_is_printable_on_its_own() -> None:
    """Most of these are read as one line in a poller report and nowhere else."""
    finding = Finding(
        task_id="task-421",
        resolved=["task-422"],
        ball=Ball.HUMAN,
        ball_reason=BallReason.DECISION,
        prompt="The epic walk stopped on task-422.",
        detail="the ask named task-422, which no longer holds a person's ball",
    )

    assert finding.describe().startswith("task-421: ")
    assert "task-422" in finding.describe()


def test_the_correction_is_logged_before_the_ball_moves(manager: TaskManager) -> None:
    """A ball that moves with no entry saying why is the thing the schema verbs exist to
    prevent, and a sweep is the one writer nobody is watching."""
    parent = epic(manager)
    first = child(manager, parent, "First")
    child(manager, parent, "Second")
    park(manager, first)
    ask_about(manager, parent, first.id, stamped=True)
    manager.close_task(first.id, actor="Jeff Posey", outcome=Outcome.COMPLETED)

    retract(
        manager.list_tasks(),
        manager.get_subtasks,
        handoff=manager.handoff,
        log=manager.add_log_entry,
    )

    corrected = manager.get_task(parent.id)
    assert corrected is not None
    explanations = [
        entry
        for entry in corrected.log
        if entry.type is LogEntryType.PROGRESS and "has been resolved" in (entry.body or "")
    ]
    assert explanations, "the sweep moved a ball without saying why"
    assert first.id in (explanations[-1].body or "")


class TestNoEligibleChildIsUsuallyAWait:
    """The stop that produced the three bounces in seven minutes.

    Every touch of task-421's parent re-dispatched a walk, which hit ``no_eligible_child``
    because the sibling was mid-finish, and handed the parent straight back to a person --
    soliciting the one action that could not help. It is a wait when something is
    actually moving, and a decision when nothing is.
    """

    def outcome(self, manager: TaskManager, parent: Task, stop, stopped_on=None):
        from agentjobs.dispatch.epic import WalkResult, record_walk_outcome

        record_walk_outcome(
            manager,
            parent.id,
            actor="dispatcher",
            result=WalkResult(
                parent_id=parent.id,
                stop=stop,
                detail="Nothing is claimable.",
                stopped_on=stopped_on,
            ),
        )
        refreshed = manager.get_task(parent.id)
        assert refreshed is not None
        return refreshed

    def test_a_sibling_somebody_is_working_is_a_wait(self, manager: TaskManager) -> None:
        from agentjobs.dispatch.epic import WalkStop, waiting_child

        parent = epic(manager)
        busy = child(manager, parent, "First")

        assert waiting_child(manager, parent.id, WalkStop.NO_ELIGIBLE_CHILD) is not None
        settled = self.outcome(manager, parent, WalkStop.NO_ELIGIBLE_CHILD)
        assert settled.ball is Ball.EXTERNAL and settled.ball_reason is BallReason.DEPENDENCY
        assert busy.id in (settled.ball_prompt or "")

    def test_re_dispatching_into_the_same_condition_writes_nothing(
        self, manager: TaskManager
    ) -> None:
        from agentjobs.dispatch.epic import WalkStop

        parent = epic(manager)
        child(manager, parent, "First")
        first_pass = self.outcome(manager, parent, WalkStop.NO_ELIGIBLE_CHILD)
        before = len(first_pass.log)

        second_pass = self.outcome(manager, parent, WalkStop.NO_ELIGIBLE_CHILD)

        assert len(second_pass.log) == before, "a re-dispatch must not ask a second time"
        assert human_waiting_tasks(manager) == []

    def test_a_graph_nothing_is_working_is_a_decision(self, manager: TaskManager) -> None:
        """Nothing clears a deadlock on its own, so this one is genuinely a person's."""
        from agentjobs.dispatch.epic import WalkStop, waiting_child

        parent = epic(manager)
        manager.create_task(
            title="Blocked",
            category="general",
            summary="Blocked.",
            description="Waits on something outside the epic.",
            lifecycle=Lifecycle.READY,
            parent=parent.id,
        )

        assert waiting_child(manager, parent.id, WalkStop.NO_ELIGIBLE_CHILD) is None
        settled = self.outcome(manager, parent, WalkStop.NO_ELIGIBLE_CHILD)
        assert settled.ball is Ball.HUMAN and settled.ball_reason is BallReason.DECISION

    def test_a_child_that_died_is_a_decision_however_many_siblings_are_moving(
        self, manager: TaskManager
    ) -> None:
        from agentjobs.dispatch.epic import WalkStop, waiting_child

        parent = epic(manager)
        child(manager, parent, "Still running")

        assert (
            waiting_child(manager, parent.id, WalkStop.CHILD_EXHAUSTED_ATTEMPTS, "task-999") is None
        )
        settled = self.outcome(manager, parent, WalkStop.CHILD_EXHAUSTED_ATTEMPTS, "task-999")
        assert settled.ball is Ball.HUMAN and settled.ball_reason is BallReason.DECISION
