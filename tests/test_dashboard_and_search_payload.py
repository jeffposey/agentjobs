"""What ``GET /dashboard`` and ``GET /search`` put on the wire (task-495).

Both answered with whole ``TaskRead`` records until this task -- every matched or listed
task's spec prose, acceptance criteria and complete log -- to draw a page of cards and a
list of results. Measured against a generated corpus of 480 on 2026-09-20: 10,888 and
10,781 bytes per record, 5.2 MB each, against the listing's 785.

``tests/test_performance_budgets.py`` holds the numbers. This file holds the *shape*, and
the two are different assertions: a budget catches a payload growing back, and a budget
alone would also pass a dashboard that had quietly stopped sending a field its cards draw.
So what is asserted here is which fields are present, which are gone, and that the
narrowed endpoint and its ``/full`` sibling still agree about what matched.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple, get_args

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.models import TaskCardRead, TaskRead, TaskSummaryRead
from agentjobs.api.routes.status import get_acting_project
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Priority, Task
from agentjobs.projects import Project
from support import task_store

#: The collections a whole record carries and a row does not. Named once, because both
#: halves of this file check the same set: the dashboard's cards and the search's rows are
#: the same projection, and a field that came back to one would come back to both.
LEFT_BEHIND = ("spec", "log", "acceptance", "deliverables", "links", "branches")


@pytest.fixture()
def api_client(tmp_path) -> Iterator[Tuple[TestClient, TaskManager]]:
    """A client over a temporary project, the way ``tests/test_api.py`` builds one."""
    reset_dependency_cache()
    manager = TaskManager(task_store(tmp_path))
    project = Project(id="test-project", name="Test Project", root=tmp_path)
    app.dependency_overrides[get_task_manager] = lambda: manager
    app.dependency_overrides[get_acting_project] = lambda: project
    with TestClient(app) as client:
        yield client, manager
    app.dependency_overrides.clear()
    reset_dependency_cache()


@pytest.fixture()
def corpus(api_client) -> List[Task]:
    """Three tasks, one of them in flight, all of them matching the word "needle"."""
    _, manager = api_client
    ready = manager.create_task(
        title="Ready and findable",
        description="Mentions the needle, and is long enough to brief an agent.",
        priority=Priority.HIGH,
        category="general",
        tags=["documentation"],
    )
    active = manager.create_task(
        title="Active and findable",
        description="Also mentions the needle.",
        category="general",
    )
    manager.promote_task(active.id, actor="Jeff Posey")
    manager.claim_task(active.id, agent="claude")
    manager.add_progress_update(active.id, author="claude", summary="Some work happened here.")
    waiting = manager.create_task(
        title="Waiting on a person, and findable",
        description="The needle is in this one too.",
        category="general",
    )
    manager.promote_task(waiting.id, actor="Jeff Posey")
    manager.handoff(
        waiting.id,
        actor="claude",
        ball="human",
        ball_reason="review",
        ball_prompt="Approve or request changes.",
    )
    return [ready, active, waiting]


def _rows(body: Any) -> List[Dict[str, Any]]:
    """Every task row in a response, whether it arrived as a list or nested in a dict."""
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    found = [row for value in body.values() if isinstance(value, list) for row in value]
    found.append(body.get("next_task"))
    return [row for row in found if isinstance(row, dict) and "id" in row]


class TestTheDashboardSendsCards:
    """Every task list on the dashboard is a card: a row, a summary line and one bit."""

    def test_the_lists_are_cards_and_the_response_model_says_so(self) -> None:
        """Asserted against the declared model as well as the body.

        The body alone would pass a route that had been widened back to whole records on
        a corpus where nothing happened to carry a log.
        """
        from agentjobs.api.models import DashboardResponse

        carried = ("active_tasks", "waiting_tasks", "backlog_tasks", "queue_preview", "next_task")
        for name in carried:
            annotation = DashboardResponse.model_fields[name].annotation
            assert TaskCardRead in get_args(annotation), f"{name} is not a card: {annotation}"

    def test_a_card_carries_the_summary_line_it_draws(self, api_client, corpus) -> None:
        client, _ = api_client
        assert corpus

        body = client.get("/api/dashboard").json()

        rows = _rows(body)
        assert rows, "the dashboard listed no tasks, so there is nothing to check"
        for row in rows:
            assert row["summary"], row["id"]
            assert isinstance(row["can_brief"], bool), row["id"]

    def test_a_card_leaves_the_record_behind(self, api_client, corpus) -> None:
        client, _ = api_client
        assert corpus

        rows = _rows(client.get("/api/dashboard").json())

        assert rows
        for row in rows:
            present = [name for name in LEFT_BEHIND if name in row]
            assert not present, (
                f"{row['id']} arrived with {present}, so the dashboard is sending records "
                "again. That was 5.2 MB at 480 tasks and grew with every log entry "
                "appended to any task (task-495)."
            )

    def test_can_brief_is_the_gate_s_own_answer(self, api_client) -> None:
        """One expression, and it is the dispatch gate's.

        The client used to compute this from the description it was being sent, so the
        two could disagree; the point of moving it is that the card cannot now say a
        task is briefable when the gate would stop to ask a person for text.
        """
        from agentjobs.dispatch.guards import record_can_brief

        client, manager = api_client
        briefable = manager.create_task(
            title="Has a working specification",
            description="Enough here to brief an agent that has never seen it.",
            category="general",
        )
        empty = manager.create_task(title="Has none", description="", category="general")

        rows = {row["id"]: row for row in _rows(client.get("/api/dashboard").json())}

        assert rows[briefable.id]["can_brief"] is record_can_brief(briefable) is True
        assert rows[empty.id]["can_brief"] is record_can_brief(empty) is False

    def test_the_recent_updates_panel_still_reads_the_log(self, api_client, corpus) -> None:
        """The one dashboard panel that needs a log still gets one.

        This is why the snapshot behind the cards still reads whole records, and why the
        cards are projected from them rather than read as rows: dropping the log from the
        payload must not drop the ten newest entries from the page.
        """
        client, _ = api_client
        assert corpus

        updates = client.get("/api/dashboard").json()["recent_updates"]

        assert updates, "the dashboard reported no recent activity for a corpus with logs"
        assert all(update["summary"] for update in updates)


class TestSearchSendsRows:
    """``GET /search`` answers with the same rows ``GET /tasks`` does."""

    def test_the_response_models_are_the_two_shapes(self) -> None:
        answers: Dict[str, Any] = {
            getattr(route, "path", ""): getattr(route, "response_model", None)
            for route in app.routes
            if getattr(route, "path", "").endswith(("/search", "/search/full"))
        }
        assert answers, "neither search route is mounted"
        for path, model in answers.items():
            expected = TaskRead if path.endswith("/full") else TaskSummaryRead
            assert get_args(model) == (expected,), f"{path} answers with {model}"

    def test_a_row_leaves_the_record_behind(self, api_client, corpus) -> None:
        client, _ = api_client
        assert corpus

        rows = client.get("/api/search", params={"q": "needle"}).json()

        assert len(rows) == 3, rows
        for row in rows:
            present = [name for name in LEFT_BEHIND if name in row]
            assert not present, f"{row['id']} arrived with {present}"

    def test_a_row_carries_the_dependency_facts(self, api_client, corpus) -> None:
        """The reason this is a read model rather than the stored record.

        Computed over the whole corpus, never over the hits: a count scoped to what the
        query matched would report 0 open children for a parent whose children it did not
        match, which is how a search once reported a six-child parent as having none.
        """
        client, manager = api_client
        parent = corpus[0]
        child = manager.create_task(
            title="A child that the query does not match",
            description="Nothing in here is worth finding.",
            category="general",
            parent=parent.id,
        )
        assert child.id

        rows = {row["id"]: row for row in client.get("/api/search", params={"q": "needle"}).json()}

        assert rows[parent.id]["open_children_count"] == 1
        assert rows[parent.id]["actionable"] is False
        assert "unmet_needs" in rows[parent.id]

    def test_full_returns_the_same_hits_in_the_same_order(self, api_client, corpus) -> None:
        client, _ = api_client
        assert corpus

        rows = client.get("/api/search", params={"q": "needle"}).json()
        records = client.get("/api/search/full", params={"q": "needle"}).json()

        assert [row["id"] for row in rows] == [record["id"] for record in records]
        assert all("log" in record for record in records)
        assert all(record["spec"]["description"] for record in records)

    @pytest.mark.parametrize("path", ("/api/search", "/api/search/full"))
    def test_a_blank_query_is_refused_by_both(self, api_client, path: str) -> None:
        client, _ = api_client
        assert client.get(path, params={"q": "   "}).status_code == 400
