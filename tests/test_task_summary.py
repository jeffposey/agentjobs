"""``TaskSummary`` is a projection of ``Task``, and these hold it to that.

The projection exists because the list endpoint was serving 10.4 MB of whole records to
draw a column of titles (task-484). Two things can go wrong with a second model of the
same thing, and neither announces itself:

-   **Drift.** A field added to ``Task`` and not here, or declared here with a different
    type, makes a listing and a record disagree about a task. ``test_fields_match``
    catches it by comparing the declarations rather than trusting a reader to notice.
-   **A second reading of the stored columns.** The store now assembles both shapes, and
    a summary built from a column it interprets differently is a row that is quietly
    wrong. ``TestAgainstWholeRecords`` builds both from the same corpus and compares
    every shared field, which is the only check that would have caught, say, ``parent``
    being read from the wrong column.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from agentjobs.manager import TaskManager
from agentjobs.dispatch.guards import record_can_brief
from agentjobs.models_v2 import (
    AUTH_RECOVERY_MARKER,
    Ball,
    BallReason,
    Lifecycle,
    Task,
    TaskCard,
    TaskSummary,
    card_of,
    summary_of,
)

from support import task_store


@pytest.fixture
def manager(tmp_path) -> TaskManager:
    """A manager over an empty SQL store, the way every other suite here builds one."""
    return TaskManager(task_store(tmp_path / "tasks"))


#: What a listing deliberately leaves behind. Named rather than derived, so removing one
#: from the projection is a decision somebody writes down.
LEFT_BEHIND = {"spec", "acceptance", "deliverables", "links", "branches", "log"}


def test_fields_match() -> None:
    """Every ``TaskSummary`` field is a ``Task`` field, declared the same way."""
    summary_fields = set(TaskSummary.model_fields)
    task_fields = set(Task.model_fields)

    invented = summary_fields - task_fields - {"self_clearing_wait"}
    assert not invented, f"TaskSummary declares fields Task does not have: {sorted(invented)}"

    missing = task_fields - summary_fields - LEFT_BEHIND
    assert not missing, (
        "a Task field is neither carried by TaskSummary nor listed in LEFT_BEHIND: "
        f"{sorted(missing)}. Add it to the projection, or name it as left behind and say "
        "which surface fetches it instead."
    )

    for name in summary_fields & task_fields:
        theirs = Task.model_fields[name]
        ours = TaskSummary.model_fields[name]
        assert ours.annotation == theirs.annotation, f"{name} is declared differently"
        assert ours.alias == theirs.alias, f"{name} has a different alias"


def test_left_behind_is_what_the_projection_actually_drops() -> None:
    """``LEFT_BEHIND`` is the real difference, not a stale list beside it."""
    assert LEFT_BEHIND == set(Task.model_fields) - set(TaskSummary.model_fields)


def _task(manager: TaskManager, **kwargs: Any) -> Task:
    """A ready task in the store, with the fields a caller cares about set."""
    kwargs.setdefault("lifecycle", Lifecycle.READY)
    kwargs.setdefault("actor", "Jeff Posey")
    kwargs.setdefault("category", "general")
    return manager.create_task(**kwargs)


class TestAgainstWholeRecords:
    """The two shapes, built from one corpus, must agree about every shared field."""

    @pytest.fixture
    def corpus(self, manager: TaskManager) -> List[Task]:
        parent = _task(
            manager,
            title="An umbrella",
            description="Holds the others.",
            priority="high",
        )
        child = _task(
            manager,
            title="Under the umbrella",
            description="A child of the parent above.",
            parent=parent.id,
            tags=["performance", "api"],
        )
        manager.claim_task(child.id, agent="claude")
        blocked = _task(
            manager,
            title="Waiting on the child",
            description="Needs the child above.",
            dependencies=[{"task": child.id, "type": "needs", "note": "the schema first"}],
        )
        return [parent, child, blocked]

    def test_every_shared_field_agrees(self, manager: TaskManager, corpus: List[Task]) -> None:
        assert corpus  # the fixture wrote them
        full = manager.storage.list_tasks()
        rows = manager.storage.list_task_summaries()

        assert [task.id for task in full] == [row.id for row in rows], "listing order differs"
        for task, row in zip(full, rows):
            for name in TaskSummary.model_fields:
                if name == "self_clearing_wait":
                    continue
                assert getattr(row, name) == getattr(task, name), f"{task.id}.{name}"

    def test_the_label_agrees(self, manager: TaskManager, corpus: List[Task]) -> None:
        """``display_status`` is derived from different amounts of record and must match.

        The label reaches for a quota wait, which a whole record derives from its log and
        a row carries as a field. That is the one derivation the projection re-homes, so
        it is the one most likely to come out different.
        """
        assert corpus
        full = {task.id: task.display_status for task in manager.storage.list_tasks()}
        rows = {row.id: row.display_status for row in manager.storage.list_task_summaries()}
        assert rows == full

    def test_summary_of_agrees_with_the_store(
        self, manager: TaskManager, corpus: List[Task]
    ) -> None:
        """Projecting a whole record gives what the store's own projection gives.

        Two implementations exist -- ``summary_of`` for a backend that can only read
        whole records, and the store's column reads -- and a backend swap must not change
        what a listing says.
        """
        assert corpus
        rows = {row.id: row for row in manager.storage.list_task_summaries()}
        for task in manager.storage.list_tasks():
            assert summary_of(task) == rows[task.id], task.id


class TestTheQuotaWait:
    """The one field a row carries rather than derives, in both directions."""

    def _park_on_a_quota_reset(self, manager: TaskManager, task_id: str) -> datetime:
        resets_at = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        manager.handoff(
            task_id,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.SERVICE,
            ball_prompt="Waiting for the usage limit to reset.",
            actor="claude",
            data={
                AUTH_RECOVERY_MARKER: {
                    "action": "park",
                    "kind": "usage_limit",
                    "resets_at": resets_at.isoformat(),
                }
            },
        )
        return resets_at

    def test_a_parked_task_carries_its_wait(self, manager: TaskManager) -> None:
        task = _task(
            manager,
            title="Parked on a quota reset",
            description="Its agent hit a usage limit.",
        )
        manager.claim_task(task.id, agent="claude")
        resets_at = self._park_on_a_quota_reset(manager, task.id)

        row = next(r for r in manager.storage.list_task_summaries() if r.id == task.id)

        assert row.ball is Ball.EXTERNAL and row.ball_reason is BallReason.SERVICE
        assert row.self_clearing_wait is not None, (
            "a row for a task parked on a quota reset lost its wait, so the list would "
            "call it 'Blocked on a service' -- a reader would open it to learn there is "
            "nothing to do, which is the cost the label exists to save"
        )
        assert row.self_clearing_wait.kind == "usage_limit"
        assert row.self_clearing_wait.resets_at is not None
        assert abs((row.self_clearing_wait.resets_at - resets_at).total_seconds()) < 1
        assert "Waiting on quota reset" in row.display_status

    def test_an_ordinary_block_carries_none(self, manager: TaskManager) -> None:
        """A genuine third-party outage must not be dressed up as a wait that clears."""
        task = _task(
            manager,
            title="Blocked on somebody else",
            description="A third party is down.",
        )
        manager.claim_task(task.id, agent="claude")
        manager.handoff(
            task.id,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.SERVICE,
            ball_prompt="Their API is returning 503.",
            actor="claude",
        )

        row = next(r for r in manager.storage.list_task_summaries() if r.id == task.id)

        assert row.self_clearing_wait is None
        assert row.display_status == "Blocked on a service"

    def test_the_newest_handoff_wins(self, manager: TaskManager) -> None:
        """A wait that was superseded by a later handoff is gone, not remembered.

        The store reads handoff rows in ascending order and lets the last one decide,
        which is the same "newest handoff" a whole record picks out of its log. An
        implementation that kept the first match would leave a reset time on a task that
        has since been parked for a different reason.
        """
        task = _task(
            manager,
            title="Parked twice",
            description="First on a quota reset, then on an outage.",
        )
        manager.claim_task(task.id, agent="claude")
        self._park_on_a_quota_reset(manager, task.id)
        # No re-claim between the two: a second handoff of a parked task is what auth
        # recovery itself does when the incident turns out to be something else.
        manager.handoff(
            task.id,
            ball=Ball.EXTERNAL,
            ball_reason=BallReason.SERVICE,
            ball_prompt="Their API is returning 503.",
            actor="claude",
        )

        row = next(r for r in manager.storage.list_task_summaries() if r.id == task.id)
        whole = manager.get_task(task.id)

        assert whole is not None
        assert row.self_clearing_wait is None
        assert row.display_status == whole.display_status == "Blocked on a service"


class TestTheListingIsCheaper:
    """The projection must not read the log, which is the whole point of it."""

    def test_no_log_rows_are_read_for_an_unparked_corpus(self, manager: TaskManager) -> None:
        """A listing of tasks nobody is waiting on touches ``log_entry`` not at all.

        Asserted against the query the store issues rather than against a duration: a
        wall-clock threshold means something different on every machine, and a listing
        that quietly went back to joining the log would still pass one on a fast day.
        """
        for index in range(3):
            created = _task(
                manager,
                title=f"Task {index}",
                description="Has a log, like every task does.",
            )
            assert created.id

        store = manager.storage
        connection = store.read_connection()
        statements: List[str] = []
        connection.set_trace_callback(statements.append)
        try:
            rows = store.list_task_summaries()
        finally:
            connection.set_trace_callback(None)

        assert len(rows) == 3
        touched_the_log = [sql for sql in statements if "log_entry" in sql]
        assert not touched_the_log, (
            "the listing joined log_entry for a corpus with nothing parked on a service: "
            f"{touched_the_log}"
        )


class TestTheCardIsTheRowPlusTwo:
    """``TaskCard`` is ``TaskSummary`` and two fields, and must stay that.

    The card exists because the dashboard draws a summary line under every title and the
    slot board needs to know whether Dispatch would stop to ask for text -- one text
    column and one boolean expression over a second (task-498). "One more field,
    everywhere" is how a projection grows back into a record, and a fourth model is only
    worth having while it is smaller than the third.
    """

    @pytest.fixture
    def corpus(self, manager: TaskManager) -> List[Task]:
        parent = _task(manager, title="An umbrella", description="Holds the others.")
        child = _task(
            manager,
            title="Under the umbrella",
            description="A child of the parent above.",
            parent=parent.id,
            priority="high",
        )
        manager.claim_task(child.id, agent="claude")
        return [parent, child]

    def test_a_card_adds_exactly_summary_and_can_brief(self) -> None:
        added = set(TaskCard.model_fields) - set(TaskSummary.model_fields)
        assert added == {"summary", "can_brief"}, (
            "TaskCard has grown past the two fields a dashboard card draws that a "
            f"listing row does not: {sorted(added)}. A surface gets the fields it "
            "draws; anything else belongs on the record it opens."
        )

    def test_the_summary_is_not_on_the_listing_row(self) -> None:
        """task-495's rejection, held. The listing must not start carrying prose."""
        assert "summary" not in TaskSummary.model_fields
        assert "can_brief" not in TaskSummary.model_fields

    def test_every_card_agrees_with_the_record_it_projects(
        self, manager: TaskManager, corpus: List[Task]
    ) -> None:
        """The store's two columns against the record's two fields, task for task."""
        assert corpus
        cards = {card.id: card for card in manager.storage.list_task_cards()}
        assert set(cards) == {task.id for task in manager.storage.list_tasks()}
        for task in manager.storage.list_tasks():
            assert cards[task.id] == card_of(task), task.id

    def test_a_task_with_no_description_cannot_brief(self, manager: TaskManager) -> None:
        """``can_brief`` is ``record_can_brief``, computed in SQL rather than in Python.

        Two expressions of one rule is how the client's copy of it drifted from the
        gate's (task-495), so this holds the column against the function itself rather
        than against a literal.
        """
        full = _task(manager, title="Briefable", description="A working specification.")
        empty = _task(manager, title="Not briefable", description="   ")
        cards = {card.id: card for card in manager.storage.list_task_cards()}
        assert cards[full.id].can_brief is record_can_brief(full)
        assert cards[empty.id].can_brief is record_can_brief(empty)
        assert (cards[full.id].can_brief, cards[empty.id].can_brief) == (True, False)

    def test_the_card_listing_is_in_the_same_order_as_the_others(
        self, manager: TaskManager, corpus: List[Task]
    ) -> None:
        assert corpus
        assert [card.id for card in manager.storage.list_task_cards()] == [
            row.id for row in manager.storage.list_task_summaries()
        ]

    def test_no_log_rows_are_read_for_an_unparked_corpus(self, manager: TaskManager) -> None:
        """The listing's assertion, on the card read. Same reasoning, same units."""
        for index in range(3):
            assert _task(manager, title=f"Task {index}", description="Has a log.").id

        store = manager.storage
        connection = store.read_connection()
        statements: List[str] = []
        connection.set_trace_callback(statements.append)
        try:
            cards = store.list_task_cards()
        finally:
            connection.set_trace_callback(None)

        assert len(cards) == 3
        touched_the_log = [sql for sql in statements if "log_entry" in sql]
        assert not touched_the_log, (
            "the card listing joined log_entry for a corpus with nothing parked on a "
            f"service: {touched_the_log}"
        )


class TestSearchIsProjectedToo:
    """``search_task_summaries`` against ``search_tasks``: same hits, same order, no log.

    The listing above is task-484's projection; this is the same relation on the search
    route, which kept answering with whole records for another day (task-495). Held here
    rather than in an API test because what must not drift is the *store's* two answers:
    a search that returned different tasks, or the same tasks in a different order, once
    it stopped reading logs would be a search that changed its answer to get faster.
    """

    @pytest.fixture
    def corpus(self, manager: TaskManager) -> List[Task]:
        first = _task(
            manager,
            title="Findable by title",
            description="Mentions the needle in its description.",
            priority="high",
        )
        second = _task(
            manager,
            title="Also findable",
            description="The needle is in here too.",
            tags=["performance"],
        )
        manager.claim_task(second.id, agent="claude")
        _task(manager, title="Unrelated", description="Nothing to find here.")
        return [first, second]

    def test_the_same_hits_in_the_same_order(
        self, manager: TaskManager, corpus: List[Task]
    ) -> None:
        assert corpus
        full = manager.storage.search_tasks("needle")
        rows = manager.storage.search_task_summaries("needle")

        assert {task.id for task in full} == {task.id for task in corpus}, (
            "the fixture's own search did not find both matches, so the comparison below "
            "would be vacuous"
        )
        # The order itself is FTS rank and is not this test's business; that the two
        # shapes produce the *same* order is.
        assert [row.id for row in rows] == [task.id for task in full]

    def test_every_shared_field_agrees(self, manager: TaskManager, corpus: List[Task]) -> None:
        assert corpus
        full = manager.storage.search_tasks("needle")
        rows = manager.storage.search_task_summaries("needle")
        for task, row in zip(full, rows):
            assert summary_of(task) == row, task.id

    def test_a_blank_query_finds_nothing_in_either_shape(self, manager: TaskManager) -> None:
        assert manager.storage.search_tasks("  ") == []
        assert manager.storage.search_task_summaries("  ") == []

    def test_no_log_rows_are_read_for_an_unparked_corpus(
        self, manager: TaskManager, corpus: List[Task]
    ) -> None:
        """The same assertion the listing makes, on the same reasoning.

        A statement count rather than a duration: a search that went back to assembling
        logs would still be under any wall-clock threshold on a fast day, and it is the
        joins rather than the milliseconds that this change removed.
        """
        assert corpus
        store = manager.storage
        connection = store.read_connection()
        statements: List[str] = []
        connection.set_trace_callback(statements.append)
        try:
            rows = store.search_task_summaries("needle")
        finally:
            connection.set_trace_callback(None)

        assert len(rows) == 2
        touched_the_log = [sql for sql in statements if "log_entry" in sql]
        assert not touched_the_log, (
            "the search joined log_entry for a corpus with nothing parked on a service: "
            f"{touched_the_log}"
        )


def test_a_summary_is_not_a_task() -> None:
    """The projection is a separate type, not a ``Task`` with its collections emptied.

    Stated as a test because it is the decision the model docstring argues for and the
    one a later refactor is most likely to undo for convenience: a subclass would carry
    an empty ``log``, and ``dispatch_count`` would then answer 0 for a task that has been
    dispatched twice -- a wrong answer where an absent field would have been a type error.
    """
    assert not issubclass(TaskSummary, Task)
    assert not issubclass(Task, TaskSummary)
    assert "log" not in TaskSummary.model_fields


def test_a_summary_rejects_a_field_it_left_behind() -> None:
    """A spec handed to the projection is refused, rather than quietly dropped.

    ``StrictModel`` forbids extras, so the refusal is inherited -- asserted here because
    the alternative reading is that a caller can pass a whole record's document and get a
    summary, and a caller who believes that will pass a log next.
    """
    document: Dict[str, Any] = {
        "schema": 2,
        "id": "task-001",
        "title": "A task",
        "created": datetime.now(tz=timezone.utc),
        "updated": datetime.now(tz=timezone.utc),
        "lifecycle": Lifecycle.READY.value,
        "ball": Ball.AGENT.value,
        "ball_reason": BallReason.AVAILABLE.value,
        "category": "general",
        "queue_position": 100,
    }
    assert TaskSummary.model_validate(document).id == "task-001"

    with pytest.raises(ValidationError):
        TaskSummary.model_validate({**document, "spec": {"summary": "s", "description": "d"}})
