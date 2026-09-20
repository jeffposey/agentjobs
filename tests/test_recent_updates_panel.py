"""The dashboard's recent-updates panel, and the read that answers it.

The panel shows the ten newest log entries in the project. It used to find them by
flattening every record's log with ``heapq.nlargest``, which meant the snapshot had to
load every record there was -- every ``log_entry`` row in the project -- to draw ten
lines (task-498). ``TaskStore.recent_log_entries`` answers it directly now.

**Replacing a sort with a query is where an answer quietly changes**, and this file is
about the ways it could. The old form's order is not "by ``ts``": ``nlargest`` is stable,
so on equal timestamps it returned entries in the order it walked them, which is
``list_tasks()`` order and then entry id within a task. Three things in a real store make
that hard to reproduce and each has a test here:

*   **Ties.** The agentjobs backlog has 25 timestamps shared by more than one entry, and
    a generated corpus can be one big tie.
*   **Two spellings of one instant.** Stored timestamps come from ``_iso``, which drops
    the fractional part when the microsecond is zero -- 106 of this repository's 5,871
    entries. ``...:17Z`` and ``...:17.000000Z`` are the same datetime and sort
    differently as text.
*   **A window that has to contain the answer.** ``...:17Z`` sorts *after* ``...:17.9Z``
    as text and before it as a datetime, so a bounded read that trusts the text order
    can push the true tenth entry out of its own window.

The oracle in each case is the implementation that was replaced, run against the same
corpus, rather than a list of ids somebody wrote down.
"""

from __future__ import annotations

import heapq
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from agentjobs.dashboard import RECENT_UPDATES_LIMIT, _collect_recent_updates
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Task
from agentjobs.sqlstore import SqlTaskStore

from support import task_store


@pytest.fixture
def store(tmp_path) -> SqlTaskStore:
    """An empty SQL store, the way every other suite here builds one."""
    return task_store(tmp_path / "tasks")


def _document(
    index: int,
    *,
    priority: str = "medium",
    entries: Sequence[Tuple[str, str]],
) -> Dict[str, Any]:
    """One ready task carrying exactly the log entries it is given.

    ``entries`` is ``(ts, body)`` pairs, written verbatim so a test can spell a timestamp
    the way the store would have stored it -- with a fractional part or without one.
    """
    return {
        "schema": 2,
        "id": f"task-{index:03d}",
        "title": f"Task {index}",
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": priority,
        "queue_position": index * 100,
        "category": "general",
        "spec": {"summary": f"Task {index} of the fixture.", "description": "Has a log."},
        "log": [
            {"id": number, "ts": ts, "actor": "claude", "type": "progress", "body": body}
            for number, (ts, body) in enumerate(entries, start=1)
        ],
    }


def _write(store: SqlTaskStore, documents: Sequence[Dict[str, Any]]) -> None:
    for document in documents:
        store.save_task(Task.model_validate(document))


def _the_old_walk(store: SqlTaskStore, limit: int = RECENT_UPDATES_LIMIT) -> List[Tuple[str, str]]:
    """What ``_collect_recent_updates`` answered before task-498, verbatim.

    ``heapq.nlargest`` over every entry of every record, keyed on ``ts`` alone, walking
    tasks in ``list_tasks()`` order and entries in entry-id order. The stability of that
    sort is the whole of the tie-break this file exists to hold the new read to, so the
    oracle is the call rather than a description of it.
    """
    newest = heapq.nlargest(
        limit,
        ((task, entry) for task in store.list_tasks() for entry in task.log),
        key=lambda pair: pair[1].ts,
    )
    return [(task.id, entry.body or "") for task, entry in newest]


def _the_new_read(store: SqlTaskStore, limit: int = RECENT_UPDATES_LIMIT) -> List[Tuple[str, str]]:
    return [(entry.task_id, entry.body or "") for entry in store.recent_log_entries(limit)]


class TestTheSameTenInTheSameOrder:
    """The read must agree with the walk it replaced, entry for entry."""

    def test_distinct_timestamps_are_simply_newest_first(self, store: SqlTaskStore) -> None:
        _write(
            store,
            [
                _document(
                    index,
                    entries=[
                        (
                            f"2026-03-0{index}T0{number}:00:00.000001Z",
                            f"task {index} entry {number}",
                        )
                        for number in range(1, 5)
                    ],
                )
                for index in range(1, 5)
            ],
        )
        assert _the_new_read(store) == _the_old_walk(store)
        assert len(_the_new_read(store)) == RECENT_UPDATES_LIMIT

    def test_a_corpus_where_every_entry_ties(self, store: SqlTaskStore) -> None:
        """One instant shared by every entry: the order is entirely the tie-break.

        The degenerate case, and not a hypothetical -- ``scripts/bench.py`` generated
        exactly this corpus until task-498. With no information in ``ts`` at all, the
        answer is decided by ``list_tasks()`` order and entry id, so a read that got the
        tie-break wrong would return ten entries from the wrong tasks and nothing about
        the timestamps would say so.

        The bands are deliberately not in id order: ``_LISTING_ORDER`` puts ``critical``
        first, so a read that fell back to ``task_id`` would disagree here and agree in
        the test above it.
        """
        tied = "2026-03-01T00:00:00.000001Z"
        _write(
            store,
            [
                _document(
                    index,
                    priority=["low", "medium", "high", "critical"][index % 4],
                    entries=[(tied, f"task {index} entry {number}") for number in range(1, 5)],
                )
                for index in range(1, 7)
            ],
        )
        assert _the_new_read(store) == _the_old_walk(store)

    def test_one_instant_spelled_two_ways(self, store: SqlTaskStore) -> None:
        """``...:00Z`` and ``...:00.000000Z`` are one datetime and two strings.

        ``_iso`` writes the first when the microsecond is zero and the second never, so
        both spellings occur in a real store. They are equal to the walk this replaced,
        which compares datetimes, and ordered by SQLite, which compares text -- so the
        tie-break has to apply to them, and a read that let SQL decide would separate
        them by whichever rows fell between the two strings.
        """
        _write(
            store,
            [
                _document(1, priority="critical", entries=[("2026-03-01T00:00:00Z", "bare")]),
                _document(
                    2, priority="critical", entries=[("2026-03-01T00:00:00.000000Z", "explicit")]
                ),
                _document(
                    3, priority="critical", entries=[("2026-03-01T00:00:00.500000Z", "halfway")]
                ),
            ],
        )
        assert _the_new_read(store) == _the_old_walk(store)

    def test_the_window_reaches_past_what_text_order_would_take(self, store: SqlTaskStore) -> None:
        """The tenth entry by text is not the tenth by datetime, and the read knows it.

        Every entry here is in one second. The whole-second entries sort *after* the
        fractional ones as text and before them as datetimes, so a read that took ten
        rows in ``ts DESC`` text order and stopped would answer with the ten oldest
        entries in that second while calling them the newest.
        """
        _write(
            store,
            [
                _document(
                    1,
                    priority="critical",
                    entries=[
                        ("2026-03-01T00:00:00Z", f"whole second {number}") for number in range(6)
                    ],
                ),
                _document(
                    2,
                    priority="critical",
                    entries=[
                        (f"2026-03-01T00:00:00.{number:06d}Z", f"fraction {number}")
                        for number in range(1, 9)
                    ],
                ),
            ],
        )
        answer = _the_new_read(store)
        assert answer == _the_old_walk(store)
        # And say out loud what the text order would have produced, so this test fails
        # for its own reason rather than because both sides changed together.
        assert [body for _, body in answer][:3] == ["fraction 8", "fraction 7", "fraction 6"]

    def test_fewer_entries_than_the_panel_shows(self, store: SqlTaskStore) -> None:
        """A project with three entries answers with three, not with an empty window."""
        _write(
            store,
            [
                _document(
                    1, entries=[("2026-03-01T00:00:0{}Z".format(n), f"e{n}") for n in range(1, 4)]
                )
            ],
        )
        assert _the_new_read(store) == _the_old_walk(store)
        assert len(_the_new_read(store)) == 3

    def test_an_empty_project_answers_with_nothing(self, store: SqlTaskStore) -> None:
        assert store.recent_log_entries(RECENT_UPDATES_LIMIT) == []

    def test_a_limit_of_zero_reads_nothing(self, store: SqlTaskStore) -> None:
        _write(store, [_document(1, entries=[("2026-03-01T00:00:01Z", "only")])])
        assert store.recent_log_entries(0) == []


class TestThePanelRendersWhatTheStoreReturned:
    """The half of ``_collect_recent_updates`` that is left: turning rows into lines."""

    def test_the_first_line_of_the_body_is_the_summary(self, store: SqlTaskStore) -> None:
        _write(store, [_document(1, entries=[("2026-03-01T00:00:01Z", "First line\nSecond line")])])
        updates = _collect_recent_updates(store.recent_log_entries(RECENT_UPDATES_LIMIT))
        assert updates[0]["summary"] == "First line"
        assert updates[0]["task_id"] == "task-001"
        assert updates[0]["task_title"] == "Task 1"
        assert updates[0]["author"] == "claude"

    def test_an_empty_body_falls_back_to_the_entry_type(self, store: SqlTaskStore) -> None:
        _write(store, [_document(1, entries=[("2026-03-01T00:00:01Z", "   ")])])
        updates = _collect_recent_updates(store.recent_log_entries(RECENT_UPDATES_LIMIT))
        assert updates[0]["summary"] == "progress"


class TestTheSnapshotAsksForTenEntriesAndNoRecords:
    """The property the panel's cost rests on, asserted where the snapshot is built."""

    def test_the_dashboard_reads_the_log_once_and_bounded(self, store: SqlTaskStore) -> None:
        from agentjobs.dashboard import build_dashboard_snapshot

        _write(
            store,
            [
                _document(
                    index,
                    entries=[
                        (
                            f"2026-03-0{index}T0{number}:00:00.000001Z",
                            f"task {index} entry {number}",
                        )
                        for number in range(1, 6)
                    ],
                )
                for index in range(1, 5)
            ],
        )
        connection = store.read_connection()
        statements: List[str] = []
        connection.set_trace_callback(statements.append)
        try:
            snapshot = build_dashboard_snapshot(TaskManager(store))
        finally:
            connection.set_trace_callback(None)

        assert len(snapshot["recent_updates"]) == RECENT_UPDATES_LIMIT
        touched_the_log = [sql for sql in statements if "log_entry" in sql]
        assert len(touched_the_log) == 2, (
            "the snapshot ran a different number of statements against log_entry than "
            "the bounded read's two: "
            + "; ".join(" ".join(sql.split())[:90] for sql in touched_the_log)
        )
        assert all("LIMIT" in sql or "ts >=" in sql for sql in touched_the_log), touched_the_log
