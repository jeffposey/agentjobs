"""The attention poll answers off listing rows, and answers the same thing (task-502).

``GET /attention`` returns 42 bytes on the real backlog and took 386-507 ms to do it. It
called ``manager.list_tasks()`` -- every record and every ``log_entry`` row in the
project -- and then ``manager.get_subtasks`` once per human-held candidate, which loads
the named task to check it exists and then filters the whole corpus by parent. With N
tasks parked on a person one poll assembled the project N+1 times, for one integer.

Two things have to hold for the fix, and this module is the second of them.
``tests/test_performance_budgets.py`` holds the first -- what the request costs, in
statements and in ``log_entry`` rows read, over a generated corpus that actually has
human-held parents. This one holds that **the answer did not change**: same waiting set,
same order, same lead, same stalls, over a corpus with a parent withdrawn by its child, a
parent not withdrawn, a plain ask and a stalled task.

The comparison is against the old code path, reconstructed here rather than described.
A test that asserted the answer the new code gives would pass whatever that answer was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Sequence, Set, Tuple

import pytest

from agentjobs.attention import AttentionState, ask_phrase, reconcile
from agentjobs.dashboard import attention_waiting, count_blocking_human, human_waiting_tasks
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, LabelledTask, Lifecycle, Task
from agentjobs.stalled import Stall, stalled_in
from support import task_store


PROJECT = "reads-rows"

#: Well past the one-hour stall threshold, so the claimed task below is quiet enough to
#: be reported whichever way its newest log timestamp is read.
LATER = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=365)


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    return TaskManager(task_store(tmp_path / "tasks"))


def make(manager: TaskManager, title: str, *, parent: str | None = None) -> Task:
    created = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=f"Do {title}.",
        lifecycle=Lifecycle.READY,
        parent=parent,
    )
    return manager.claim_task(created.id, agent="claude")


def park(manager: TaskManager, task: Task, reason: BallReason = BallReason.REVIEW) -> Task:
    return manager.handoff(
        task.id,
        actor="claude",
        ball=Ball.HUMAN,
        ball_reason=reason,
        ball_prompt="Read it and say whether it goes in.",
    )


def ids(tasks: Sequence[LabelledTask]) -> List[str]:
    return [task.id for task in tasks]


#: The waiting set the corpus below produces, named so the assertions can say what they
#: are counting rather than repeating a number nobody can check.
WAITING = 5
HUMAN_HELD = 3


@pytest.fixture()
def corpus(manager: TaskManager) -> TaskManager:
    """Every shape the answer depends on, in one project.

    *   ``withdrawn`` -- a parent whose child holds the ask. The click is on the child,
        so the parent is **not** a second thing to do (task-467), and a children lookup
        that answered "none" for everything would quietly put it back.
    *   ``standing`` -- a parent whose child an agent is working, which withdraws
        nothing. Both, because a lookup that answered "a human-held child" for
        everything would agree with the first case and be wrong about this one.
    *   ``plain`` -- an ask with no children at all.
    *   ``stalled`` -- claimed, quiet, and nothing in the run ledger: in the waiting set
        by :mod:`agentjobs.stalled` rather than by its ball, which reads ``agent``.
    """
    withdrawn = make(manager, "Withdrawn parent")
    park(manager, make(manager, "Child holding the ask", parent=withdrawn.id))
    park(manager, withdrawn, BallReason.DECISION)

    standing = make(manager, "Standing parent")
    make(manager, "Child an agent is working", parent=standing.id)
    park(manager, standing)

    park(manager, make(manager, "Plain ask"), BallReason.APPROVAL)
    make(manager, "Stalled")
    manager.withdrawn_parent_id = withdrawn.id  # type: ignore[attr-defined]
    return manager


def the_old_answer(
    manager: TaskManager, home: Path, now: datetime
) -> Tuple[List[Task], List[Stall]]:
    """``reconcile``'s waiting set and stalls as the code before task-502 computed them.

    Whole records, the detector reading each candidate's newest timestamp off the record
    it was handed, and ``manager.get_subtasks`` -- a corpus read per candidate -- as the
    children lookup. Reconstructed rather than described, so that "the answer is the
    same" is a comparison and not a restatement of whatever the new code does.
    """
    tasks = manager.list_tasks()
    stalls = stalled_in(tasks, project_id=PROJECT, home=home, now=now)
    waiting = attention_waiting(tasks, manager.get_subtasks, {stall.task_id for stall in stalls})
    return waiting, stalls


def the_new_answer(manager: TaskManager, home: Path, now: datetime) -> AttentionState:
    return reconcile(manager, PROJECT, home=home, now=now)


class TestTheAnswerIsUnchanged:
    """Same set, same order, same lead, same stalls."""

    def test_the_waiting_set_and_its_order_agree(self, corpus: TaskManager, tmp_path: Path) -> None:
        expected, _stalls = the_old_answer(corpus, tmp_path, LATER)
        state = the_new_answer(corpus, tmp_path, LATER)

        assert ids(list(state.waiting)) == ids(expected)
        assert state.blocking == len(expected)
        # The corpus is worth having only if both branches of the children lookup are
        # really in it, so say what it holds rather than trusting that it still does: a
        # lookup that answered "no children" for everything would put the withdrawn
        # parent back and this count would be six.
        assert len(expected) == WAITING
        assert corpus.withdrawn_parent_id not in ids(expected)  # type: ignore[attr-defined]

    def test_the_episode_members_and_lead_agree(self, corpus: TaskManager, tmp_path: Path) -> None:
        expected, _stalls = the_old_answer(corpus, tmp_path, LATER)
        state = the_new_answer(corpus, tmp_path, LATER)

        assert state.episode is not None
        assert list(state.episode.members) == ids(expected)
        # What the notification prints, which is the part of the answer a row has to
        # carry: the lead's id, its title and the phrase its ball reason renders as.
        lead, was = state.waiting[0], expected[0]
        assert (lead.id, lead.title, ask_phrase(lead)) == (was.id, was.title, ask_phrase(was))

    def test_the_stalls_agree(self, corpus: TaskManager, tmp_path: Path) -> None:
        _expected, stalls = the_old_answer(corpus, tmp_path, LATER)
        state = the_new_answer(corpus, tmp_path, LATER)

        assert list(state.stalls) == stalls
        assert [stall.task_id for stall in stalls], "the corpus holds no stall to compare"

    def test_the_badge_agrees_with_the_poll_less_the_stalls(
        self, corpus: TaskManager, tmp_path: Path
    ) -> None:
        """``count_blocking_human`` moved to rows in the same change, so hold it here.

        It is the *human-held* half of the set -- the legacy Jinja header's number -- so
        it is the poll's count without the stalled task, and one predicate decides both.
        """
        state = the_new_answer(corpus, tmp_path, LATER)
        stalled_only = {stall.task_id for stall in state.stalls} - {
            task.id for task in state.waiting if task.ball is Ball.HUMAN
        }

        assert count_blocking_human(corpus) == state.blocking - len(stalled_only)
        assert (
            set(ids(human_waiting_tasks(corpus)))
            == {task.id for task in state.waiting} - stalled_only
        )


class SpyingManager:
    """A manager that records the whole-record reads made through it.

    The parity tests above would pass over a corpus this small however the answer was
    reached, because four tasks read four hundred times still give the right answer. This
    is the shape assertion: what ``reconcile`` may read is the listing projection and a
    bounded timestamp lookup, and the two whole-record methods it used to call are the
    ones the budgets caught at 480 records.
    """

    def __init__(self, inner: TaskManager) -> None:
        self._inner = inner
        self.whole_record_reads: List[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name in {"list_tasks", "get_subtasks", "get_task"}:

            def recorded(*args: Any, **kwargs: Any) -> Any:
                self.whole_record_reads.append(name)
                return attribute(*args, **kwargs)

            return recorded
        return attribute


class TestWhatThePollReads:
    def test_it_reads_no_whole_records_at_all(self, corpus: TaskManager, tmp_path: Path) -> None:
        spy = SpyingManager(corpus)
        state = reconcile(spy, PROJECT, home=tmp_path, now=LATER)  # type: ignore[arg-type]

        assert state.blocking == WAITING
        assert spy.whole_record_reads == [], (
            "the attention poll read whole records: "
            f"{spy.whole_record_reads}. Everything it needs is on a listing row except "
            "each candidate's newest log timestamp, and `newest_log_ts` is that."
        )

    def test_the_badge_reads_no_whole_records_either(self, corpus: TaskManager) -> None:
        spy = SpyingManager(corpus)

        assert count_blocking_human(spy) == HUMAN_HELD  # type: ignore[arg-type]
        assert spy.whole_record_reads == [], (
            "the legacy Jinja header's badge read whole records: "
            f"{spy.whole_record_reads}. `web.py` calls it once per page."
        )

    def test_the_candidates_are_the_only_log_read(
        self, corpus: TaskManager, tmp_path: Path
    ) -> None:
        """``newest_log_ts`` is asked about the stall candidates and nobody else.

        The bound the budget asserts in rows, asserted here in ids -- so a change that
        started resolving timestamps for the whole waiting set, or for the corpus, fails
        beside the code rather than only at 480 generated records.
        """
        asked: List[Set[str]] = []
        inner = corpus.newest_log_ts

        def recorded(task_ids: Sequence[str]) -> Any:
            asked.append(set(task_ids))
            return inner(task_ids)

        corpus.newest_log_ts = recorded  # type: ignore[method-assign]
        state = reconcile(corpus, PROJECT, home=tmp_path, now=LATER)

        claimed = {
            task.id
            for task in corpus.list_task_summaries()
            if task.lifecycle is Lifecycle.ACTIVE and task.ball is Ball.AGENT
        }
        assert asked == [claimed]
        assert {stall.task_id for stall in state.stalls} <= claimed
