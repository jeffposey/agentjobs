"""The request-scoped corpus snapshot: what it keeps, and what it must never keep.

Task-485 found ``GET /dashboard`` loading every task in the project nine times to
answer one request -- 1.6 of the 2 seconds it took on an idle machine. The scope in
:mod:`agentjobs.corpus` is the fix, and it is a memo with a lifetime, which is exactly
the kind of thing that is correct on the day it is written and wrong six months later.

So the cases here are mostly about the lifetime rather than the saving:

* a read inside a scope is served once and the rest come from that load;
* a **write** inside a scope discards it, so a handler cannot read its own corpus stale;
* the scope does not outlive its block, so no later caller inherits it;
* ``list_tasks_uncached`` ignores it, because the queue's own mutations re-read to
  check what they just wrote.

The last one is not hypothetical. ``tests/test_dispatch_epic.py`` carries the epitaph
of the previous memo, which was scoped to a *CLI invocation*: an epic walk is a single
invocation that runs for as long as the epic does, and it saw every child exactly as it
was when the walk began, forever. Nothing outside one HTTP request opens this one.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

from agentjobs.corpus import corpus_scope, discard, memoised, scope_is_open
from agentjobs.instrumentation import count_task_parses
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    Lifecycle,
    Priority,
    Spec,
    Task,
)
from agentjobs.sqlstore import Database, SqlTaskStore, upgrade


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqlTaskStore]:
    """A migrated store holding three tasks."""
    database = Database(tmp_path / "agentjobs.db")
    upgrade(database, agentjobs_version="test", snapshot_before=False)
    task_store = SqlTaskStore(database, "demo")
    task_store.ensure_project(root=str(tmp_path), reporting_tz="America/Chicago")
    for index in range(1, 4):
        task_store.save_task(make_task(f"task-{index:03d}", queue_position=index * 100))
    yield task_store
    database.close()


def make_task(task_id: str, **overrides) -> Task:
    """A minimal valid task, so each test states only what it is about."""
    now = datetime.now(tz=timezone.utc)
    fields = {
        "id": task_id,
        "title": f"Title for {task_id}",
        "created": now,
        "updated": now,
        "lifecycle": Lifecycle.READY,
        "ball": Ball.AGENT,
        "ball_reason": BallReason.AVAILABLE,
        "priority": Priority.MEDIUM,
        "queue_position": 100,
        "category": "engineering",
        "spec": Spec(summary="A summary.", description="A description."),
    }
    fields.update(overrides)
    return Task(**fields)


class TestWhatTheScopeSaves:
    """The saving itself, asserted as a count rather than as a duration."""

    def test_repeated_reads_inside_one_scope_load_once(self, store: SqlTaskStore) -> None:
        with count_task_parses() as tally:
            with corpus_scope():
                for _ in range(5):
                    assert len(store.list_tasks()) == 3
        assert tally.corpus_loads == 1, (
            f"Five reads inside one scope loaded the corpus {tally.corpus_loads} times. "
            "The scope exists so that the nine questions a dashboard asks the corpus "
            "share one load (task-485)."
        )

    def test_without_a_scope_every_read_loads(self, store: SqlTaskStore) -> None:
        """No scope, no memo. A CLI process must not inherit a frozen corpus."""
        with count_task_parses() as tally:
            for _ in range(3):
                store.list_tasks()
        assert tally.corpus_loads == 3

    def test_the_answer_is_the_same_either_way(self, store: SqlTaskStore) -> None:
        """A memo that changed the answer would be a bug, not an optimisation."""
        without = [task.id for task in store.list_tasks()]
        with corpus_scope():
            store.list_tasks()
            within = [task.id for task in store.list_tasks()]
        assert within == without

    def test_each_caller_gets_its_own_list(self, store: SqlTaskStore) -> None:
        """One caller sorting its answer in place must not reorder the next one's."""
        with corpus_scope():
            first = store.list_tasks()
            first.reverse()
            del first[0]
            assert [task.id for task in store.list_tasks()] == [
                "task-001",
                "task-002",
                "task-003",
            ]


class TestWhatTheScopeMustNotKeep:
    """The lifetime rules. Every one of these is a way a cache goes wrong."""

    def test_a_write_inside_a_scope_discards_it(self, store: SqlTaskStore) -> None:
        """A handler that saves and then reads must see what it just saved."""
        with corpus_scope():
            assert len(store.list_tasks()) == 3
            store.save_task(make_task("task-004", queue_position=400))
            assert [task.id for task in store.list_tasks()] == [
                "task-001",
                "task-002",
                "task-003",
                "task-004",
            ]

    def test_a_delete_inside_a_scope_discards_it(self, store: SqlTaskStore) -> None:
        with corpus_scope():
            assert len(store.list_tasks()) == 3
            assert store.delete_task("task-002") is True
            assert [task.id for task in store.list_tasks()] == ["task-001", "task-003"]

    def test_a_later_scope_starts_from_the_database(self, store: SqlTaskStore) -> None:
        """Two scopes are two requests, and the second is entitled to the truth."""
        with corpus_scope():
            assert len(store.list_tasks()) == 3
        store.save_task(make_task("task-004", queue_position=400))
        with count_task_parses() as tally:
            with corpus_scope():
                assert len(store.list_tasks()) == 4
        assert tally.corpus_loads == 1

    def test_the_scope_is_closed_outside_its_block(self, store: SqlTaskStore) -> None:
        with corpus_scope():
            assert scope_is_open()
        assert not scope_is_open()

    def test_a_thread_that_outlives_the_scope_reads_the_database(self, store: SqlTaskStore) -> None:
        """A context copied into a thread must not pin the corpus the scope held.

        A dispatch watcher started inside a request keeps a copy of that request's
        context. Copied contexts point at the *same* snapshot object, so a snapshot
        that merely went out of the request's scope would go on answering that thread
        with a corpus from whenever the request ran.
        """
        seen: List[int] = []
        release = threading.Event()
        started = threading.Event()

        def watcher() -> None:
            started.set()
            release.wait(timeout=10)
            seen.append(len(store.list_tasks()))

        with corpus_scope():
            store.list_tasks()
            thread = threading.Thread(target=watcher)
            thread.start()
            started.wait(timeout=10)

        store.save_task(make_task("task-004", queue_position=400))
        release.set()
        thread.join(timeout=10)
        assert seen == [4], "the thread read a corpus its scope had already closed"

    def test_an_exception_still_closes_the_scope(self, store: SqlTaskStore) -> None:
        with pytest.raises(RuntimeError):
            with corpus_scope():
                store.list_tasks()
                raise RuntimeError("handler blew up")
        assert not scope_is_open()


class TestTheUncachedRead:
    """``list_tasks_uncached`` is what the queue's own mutations read through."""

    def test_it_ignores_an_open_scope(self, store: SqlTaskStore) -> None:
        with count_task_parses() as tally:
            with corpus_scope():
                store.list_tasks()
                store.list_tasks_uncached()
                store.list_tasks_uncached()
        assert tally.corpus_loads == 3

    def test_it_sees_a_write_the_memo_predates(self, store: SqlTaskStore) -> None:
        """The case it exists for: re-read to check what you just wrote."""
        with corpus_scope():
            store.list_tasks()
            store.database.writer.execute(
                "UPDATE task SET queue_position = 999 "
                "WHERE project_id = 'demo' AND task_id = 'task-001'"
            )
            positions = {task.id: task.queue_position for task in store.list_tasks_uncached()}
        assert positions["task-001"] == 999


class TestTheScopeItself:
    """The primitive, away from the store, because two callers already depend on it."""

    def test_it_is_re_entrant(self) -> None:
        """A nested scope defers rather than starting a second snapshot."""
        calls: List[int] = []

        def produce() -> int:
            calls.append(1)
            return len(calls)

        with corpus_scope():
            assert memoised("k", produce) == 1
            with corpus_scope():
                assert memoised("k", produce) == 1
            assert memoised("k", produce) == 1
        assert len(calls) == 1

    def test_discard_outside_a_scope_is_harmless(self) -> None:
        """Called from a write in a CLI process, where there is nothing to discard."""
        discard()
        assert not scope_is_open()

    def test_keys_do_not_collide(self) -> None:
        with corpus_scope():
            assert memoised("a", lambda: "first") == "first"
            assert memoised("b", lambda: "second") == "second"
            assert memoised("a", lambda: "third") == "first"


class TestTheApplicationOpensOne:
    """Every case above passes with the middleware deleted. This one does not.

    The scope is opened in exactly one place -- ``api.main.measure_request`` -- and the
    saving is entirely a consequence of that. Asserting on the header rather than on a
    duration, for the reason ``test_performance_budgets`` states at length: a count
    means the same thing on every machine, and a threshold does not.

    The numeric budget that would catch a *regression* here belongs to task-485's
    sibling, which sets it against post-fix behaviour. This asserts only that a
    request opens a scope at all.
    """

    def test_one_request_loads_the_corpus_once(self, tmp_path: Path, monkeypatch) -> None:
        import yaml
        from fastapi.testclient import TestClient

        from agentjobs.api.dependencies import reset_dependency_cache
        from agentjobs.api.main import CORPUS_LOAD_HEADER, app
        from agentjobs.project_setup import build_project_config
        from support import task_store as store_for

        config = tmp_path / ".agentjobs" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            yaml.safe_dump(
                build_project_config(project_name="Scope project", user="Scope Human"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        store = store_for(tmp_path / "tasks", root=tmp_path)
        for index in range(1, 6):
            store.save_task(make_task(f"task-{index:03d}", queue_position=index * 100))

        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
        reset_dependency_cache()
        try:
            client = TestClient(app)
            for path in ("/api/projects/_local/dashboard", "/api/projects/_local/tasks/next"):
                response = client.get(path)
                assert response.status_code == 200, response.text
                loads = int(response.headers[CORPUS_LOAD_HEADER])
                assert loads == 1, (
                    f"{path} loaded the corpus {loads} times. One request is one scope, "
                    "and the dashboard asked nine separate questions of it before "
                    "task-485."
                )
            revision = client.get("/api/projects/_local/revision")
            assert revision.status_code == 200
            assert int(revision.headers[CORPUS_LOAD_HEADER]) == 0, (
                "The 15-second poll loaded the corpus. It answers 'has anything "
                "changed' from a counter and must load nothing at all."
            )
        finally:
            reset_dependency_cache()
