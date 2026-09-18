"""``GET /api/projects/{id}/analytics`` -- the endpoint ``docs/analytics-design.md`` §7 specifies.

The corpus here is planted event by event at chosen instants, as rows the product could
have written, because every claim this page makes is about *when* something happened. A
fixture that let the clock fall where it liked could not fail for any of the reasons
these tests exist:

*   **§3.5, the timezone.** Two instants are placed at 05:30 UTC -- one in July, one in
    December -- because that is the hour where a fixed offset and a real zone disagree,
    and where every naive implementation of this page has been wrong for half of each
    year.
*   **§3.2, the invariant.** ``SUM(open_delta)`` equalling the open count is the single
    assertion that catches a history which has quietly stopped adding up, and §6.1 item D
    names it a dependency rather than a suggestion. ``tests/test_analytics_contract.py``
    holds it at the store; here it is held through the API's own read path, because the
    page is what goes visibly wrong when it fails.
*   **§5.3, the reconciliation.** The analytics totals and
    ``build_dashboard_snapshot()["stats"]`` are two independent statements of the same
    six rules -- SQL in ``agentjobs.analytics``, Python in ``agentjobs.dashboard``. The
    test is what stops them drifting apart silently, and it is only worth anything
    *because* they are separate implementations: had the endpoint called the dashboard's
    own function, this file would contain a comparison of a value with itself.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.analytics import (
    QUERIES,
    AnalyticsProjection,
    bucket_start,
    local_day,
    percentile,
    spine,
)
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dashboard import build_dashboard_snapshot
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
from agentjobs.sqlstore import SqlTaskStore
from support import task_store

CHICAGO = ZoneInfo("America/Chicago")

#: The instant this suite calls "now". Fixed, because every age, dwell and range in the
#: payload is measured from it, and a suite whose expected numbers moved with the wall
#: clock would be rewritten weekly instead of read.
NOW = datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    """The stored timestamp form -- ``Z``-suffixed UTC, as ``sqlstore`` writes it."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slot(task_id: str) -> int:
    """A queue position no other task in this fixture will take."""
    digits = "".join(character for character in task_id if character.isdigit())
    return int(digits or "1") * 100


def plant_task(
    store: SqlTaskStore,
    task_id: str,
    *,
    created: datetime,
    closed: Optional[datetime] = None,
    outcome: str = "completed",
    ball: str = "agent",
    ball_reason: str = "available",
    ball_since: Optional[datetime] = None,
    archived: bool = False,
    priority: str = "medium",
    position: Optional[int] = None,
    source: str = "native",
) -> None:
    """Write one task and its history at instants the test chooses.

    Straight SQL, for the reason ``test_sqlstore_analytics`` gives: these cases are
    about the shape of the history the queries see, and going through the model would
    spend the budget on validation they do not test. The rows still satisfy every
    ``CHECK`` in ``001_initial.sql``, so a corpus this builds is one the product could
    have written.
    """
    closed_text = iso(closed) if closed is not None else None
    updated_text = closed_text or iso(created)
    is_closed = closed is not None
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO task(project_id, task_id, title, created_at, updated_at,"
            " lifecycle, ball, ball_reason, ball_prompt, outcome, archived, priority,"
            " queue_position, category, spec_summary, spec_description, closed_at,"
            " last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                task_id,
                f"Title of {task_id}",
                iso(created),
                updated_text,
                "closed" if is_closed else "ready",
                None if is_closed else ball,
                None if is_closed else ball_reason,
                None if is_closed or ball_reason == "available" else "Do the thing.",
                outcome if is_closed else None,
                1 if archived else 0,
                priority,
                # ``ux_task_queue_slot`` makes two open tasks sharing a slot
                # unrepresentable, so a fixture cannot hand every one the same number.
                None if is_closed else (position if position is not None else _slot(task_id)),
                "engineering",
                f"Summary of {task_id}.",
                f"Description of {task_id}.",
                closed_text,
                updated_text,
            ),
        )
        connection.execute(
            "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
            " lifecycle_to, ball_to, ball_reason_to, archived_to, priority_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                task_id,
                iso(created),
                "claude",
                "create",
                source,
                "ready",
                ball,
                ball_reason,
                1 if archived else 0,
                priority,
            ),
        )
        if ball_since is not None:
            # A handoff into the ball the task now holds, so "days held" has something
            # later than the creation to measure from.
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_from, lifecycle_to, ball_from, ball_to,"
                " ball_reason_from, ball_reason_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    store.project_id,
                    task_id,
                    iso(ball_since),
                    "claude",
                    "handoff",
                    source,
                    "ready",
                    "ready",
                    "agent",
                    ball,
                    "available",
                    ball_reason,
                ),
            )
        if is_closed:
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_from, lifecycle_to, ball_from, ball_to, outcome_to)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    store.project_id,
                    task_id,
                    closed_text,
                    "claude",
                    "close",
                    source,
                    "ready",
                    "closed",
                    ball,
                    None,
                    outcome,
                ),
            )


def build_project(
    root: Path, project_id: str, *, timezone_name: str = "America/Chicago"
) -> SqlTaskStore:
    """A registered project directory with an empty store in the reporting zone."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": project_id, "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    store = task_store(root / "tasks", project_id=project_id)
    with store.database.write() as connection:
        connection.execute(
            "UPDATE project SET reporting_tz = ? WHERE project_id = ?",
            (timezone_name, project_id),
        )
    return store


def seed_history(store: SqlTaskStore) -> None:
    """A small corpus with every state the panels have to render.

    Deliberately not random. Each task below exists to make one assertion possible, and
    the numbers in the tests are counted from this function rather than from a run.

    **Seeded from the real clock, not from `NOW`.** This corpus is read back over HTTP
    by the `served` fixture, and the endpoint has no `now` to inject -- it ages
    everything against the wall clock. Seeding from a fixed instant therefore asserted
    that the wall clock *is* that instant: `age_days` came out as 120 plus however far
    today has drifted from 18 Sep 2026 17:00 UTC, and `pytest.approx(120, abs=0.1)`
    gave the suite a 4.8-hour window in which it was green. Ages are relative in every
    assertion that reads this corpus, so the reference only has to be the same one the
    endpoint uses. `NOW` stays for the projection tests, which inject it.
    """
    reference = datetime.now(timezone.utc)
    day = lambda n: reference - timedelta(days=n)  # noqa: E731 - a local shorthand, read once

    # Four completed, spread over three weeks, one of them reopened and closed again.
    for index, (age, closed_age) in enumerate([(60, 40), (50, 30), (35, 20), (30, 8)], start=1):
        plant_task(
            store,
            f"task-{index:03d}",
            created=day(age),
            closed=day(closed_age),
        )
    # One cancelled: closed, and deliberately not completed (section 3.3).
    plant_task(store, "task-005", created=day(45), closed=day(15), outcome="cancelled")

    # Open work, in every band the aging panel draws.
    plant_task(store, "task-006", created=day(2), ball="agent", ball_reason="available")
    plant_task(
        store, "task-007", created=day(12), ball="agent", ball_reason="work", ball_since=day(3)
    )
    plant_task(
        store, "task-008", created=day(45), ball="human", ball_reason="review", ball_since=day(20)
    )
    plant_task(
        store, "task-009", created=day(120), ball="human", ball_reason="spec", ball_since=day(100)
    )
    plant_task(
        store,
        "task-010",
        created=day(70),
        ball="external",
        ball_reason="dependency",
        ball_since=day(10),
    )
    # Archived and open: it counts in the level (section 3.4) and in nothing else.
    plant_task(
        store, "task-011", created=day(200), ball="agent", ball_reason="available", archived=True
    )


@pytest.fixture()
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, SqlTaskStore, TaskManager]]:
    """One server, two registered projects, real wiring rather than overrides.

    The real registry for the same reason ``test_api_multiproject`` uses it: the scoped
    mount resolving the addressed project is half of ac-1, and a dependency override
    would answer with the same store for every project id and prove nothing.
    """
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    store = build_project(tmp_path / "alpha", "alpha")
    seed_history(store)
    build_project(tmp_path / "fresh", "fresh")

    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / "alpha", project_id="alpha")
    registry.add(tmp_path / "fresh", project_id="fresh")

    with TestClient(app) as client:
        yield client, store, TaskManager(store)

    reset_dependency_cache()


def analytics(client: TestClient, project: str = "alpha", **params: Any) -> Dict[str, Any]:
    response = client.get(f"/api/projects/{project}/analytics", params=params)
    assert response.status_code == 200, response.text
    payload: Dict[str, Any] = response.json()
    return payload


# ---------------------------------------------------------------------------


class TestTheEndpoint:
    """ac-1 -- the §7.3 shape, at both mounts, with named models behind it."""

    def test_it_is_mounted_at_both_spellings(self, served) -> None:
        """``PROJECT_SCOPED_ROUTERS`` gets every task-facing router mounted twice."""
        client, _store, _manager = served

        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/analytics" in paths
        assert "/api/projects/{project_id}/analytics" in paths

    def test_the_unscoped_mount_serves_the_default_project(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """One project registered, so the default resolves without ambiguity.

        Separate from the two-project fixture on purpose: with two registered projects
        and a working directory inside neither, ``/api/analytics`` is a 409 for every
        router in the application, which is a fact about default resolution rather than
        about this endpoint.
        """
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
        monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_dependency_cache()
        store = build_project(tmp_path / "solo", "solo")
        seed_history(store)
        ProjectRegistry(home=tmp_path / "home").add(tmp_path / "solo", project_id="solo")

        with TestClient(app) as client:
            response = client.get("/api/analytics")

        reset_dependency_cache()
        assert response.status_code == 200, response.text
        assert response.json()["totals"]["total"] == 11

    def test_the_response_carries_every_section_of_the_page(self, served) -> None:
        client, _store, _manager = served

        scoped = client.get("/api/projects/alpha/analytics")

        assert scoped.status_code == 200
        assert set(scoped.json()) == {
            "range",
            "coverage",
            "totals",
            "backlog",
            "holders",
            "throughput",
            "aging",
            "oldest",
            "stuck",
        }

    def test_the_scoped_mount_answers_for_the_project_it_names(self, served) -> None:
        """Not for the default one -- which is what a shared handler gets wrong."""
        client, _store, _manager = served

        alpha = analytics(client, "alpha")
        fresh = analytics(client, "fresh")

        assert alpha["totals"]["total"] == 11
        assert fresh["totals"]["total"] == 0

    @pytest.mark.parametrize("key", ["30d", "90d", "12m", "all"])
    def test_every_range_preset_is_served(self, served, key: str) -> None:
        client, _store, _manager = served
        assert analytics(client, range=key)["range"]["key"] == key

    def test_the_default_range_is_ninety_days(self, served) -> None:
        client, _store, _manager = served
        assert analytics(client)["range"]["key"] == "90d"

    def test_a_range_outside_the_four_presets_is_refused(self, served) -> None:
        """§7.1: the four presets are the whole surface, so ``7d`` is a bad request."""
        client, _store, _manager = served
        assert (
            client.get("/api/projects/alpha/analytics", params={"range": "7d"}).status_code == 400
        )

    def test_an_unknown_project_is_the_scoped_routers_own_404(self, served) -> None:
        client, _store, _manager = served
        assert client.get("/api/projects/nope/analytics").status_code == 404

    def test_every_response_model_is_named_in_the_document(self, served) -> None:
        """An inline ``dict`` would generate anonymous TypeScript (§7.2).

        The generated client is part of the gate, so a model that lost its name here
        would reach the page as an unnameable inline type and be found by whoever tried
        to write a prop type for it.
        """
        client, _store, _manager = served

        schemas = client.get("/openapi.json").json()["components"]["schemas"]

        assert {
            "AnalyticsResponse",
            "AnalyticsRange",
            "AnalyticsCoverage",
            "AnalyticsTotals",
            "BacklogPoint",
            "HolderPoint",
            "ThroughputPoint",
            "AgeBucket",
            "AgingTask",
            "StuckGroup",
        } <= set(schemas)

    def test_the_endpoint_parses_no_task_document(self, served) -> None:
        """The whole reason the page waited for the store (§5.1).

        ``X-Task-Parses`` is the standing assertion that no request has quietly started
        reading a directory again, and on this endpoint it is the acceptance criterion
        rather than a nicety.
        """
        client, _store, _manager = served

        response = client.get("/api/projects/alpha/analytics")

        assert response.headers["X-Task-Parses"] == "0"


class TestTimezoneBuckets:
    """ac-2 -- days are local days, computed with ``zoneinfo``, across DST."""

    @pytest.mark.parametrize(
        "instant, expected",
        [
            # Late evening in Chicago, either side of the DST boundary. A fixed -06:00
            # gets the first wrong; a fixed -05:00 gets the second wrong. §3.5's table.
            (datetime(2026, 7, 4, 5, 30, tzinfo=timezone.utc), date(2026, 7, 4)),
            (datetime(2026, 12, 24, 5, 30, tzinfo=timezone.utc), date(2026, 12, 23)),
        ],
    )
    def test_a_late_evening_instant_lands_on_its_local_day(
        self, instant: datetime, expected: date
    ) -> None:
        assert local_day(instant, CHICAGO) == expected

    @pytest.mark.parametrize(
        "instant, expected",
        [
            (datetime(2026, 7, 4, 5, 30, tzinfo=timezone.utc), date(2026, 7, 4)),
            (datetime(2026, 12, 24, 5, 30, tzinfo=timezone.utc), date(2026, 12, 24)),
        ],
    )
    def test_the_same_instants_are_utc_days_in_a_utc_project(
        self, instant: datetime, expected: date
    ) -> None:
        """The zone is doing the work, not an accident of the arithmetic."""
        assert local_day(instant, ZoneInfo("UTC")) == expected

    def test_the_series_files_an_event_on_the_local_day_it_happened(self, tmp_path: Path) -> None:
        """End to end, through the projection, in both halves of the year.

        Two creations at 05:30 UTC, six months apart. In Chicago the July one belongs to
        the 4th and the December one to the 23rd; ``date(ts,'-06:00')`` would put the
        first on the 3rd and ``date(ts,'-05:00')`` the second on the 24th.
        """
        store = build_project(tmp_path / "zones", "zones")
        plant_task(store, "task-001", created=datetime(2026, 7, 4, 5, 30, tzinfo=timezone.utc))
        plant_task(store, "task-002", created=datetime(2026, 12, 24, 5, 30, tzinfo=timezone.utc))

        payload = AnalyticsProjection(
            store.read_connection(),
            "zones",
            now=datetime(2027, 1, 5, 12, 0, tzinfo=timezone.utc),
        ).build("all")

        arrivals = {point["day"]: point["opened"] for point in payload["backlog"]}
        assert arrivals[bucket_start(date(2026, 7, 4), "week")] == 1
        assert arrivals[bucket_start(date(2026, 12, 23), "week")] == 1
        assert payload["range"]["timezone"] == "America/Chicago"

    def test_the_zone_a_project_reports_in_is_on_every_response(self, served) -> None:
        client, _store, _manager = served
        assert analytics(client)["range"]["timezone"] == "America/Chicago"


class TestTheSpine:
    """ac-3 -- a filled calendar from the coverage baseline, marked where it is guessed."""

    def test_every_bucket_in_the_range_is_present(self, served) -> None:
        """§3.5: 8% of days carry an event, so 92% of the chart is this.

        Grouped rows would leave the other days out and let a reader interpolate across
        them, which is a claim about days nobody measured.
        """
        client, _store, _manager = served

        payload = analytics(client, range="90d")
        days = [date.fromisoformat(point["day"]) for point in payload["backlog"]]

        assert days == sorted(days)
        assert len(set(days)) == len(days)
        assert all(later - earlier == timedelta(days=1) for earlier, later in zip(days, days[1:]))

    def test_the_holders_share_the_backlog_spine(self, served) -> None:
        client, _store, _manager = served

        payload = analytics(client, range="90d")

        assert [point["day"] for point in payload["holders"]] == [
            point["day"] for point in payload["backlog"]
        ]

    def test_the_series_starts_at_the_coverage_baseline_not_at_the_epoch(self, served) -> None:
        """§3.6 rule 1. ``all`` means "as far back as the store will speak for"."""
        client, _store, _manager = served

        payload = analytics(client, range="all")
        baseline = datetime.fromisoformat(payload["coverage"]["baseline_at"])
        first = date.fromisoformat(payload["backlog"][0]["day"])

        assert first == bucket_start(local_day(baseline, CHICAGO), payload["range"]["bucket"])

    def test_a_range_reaching_past_the_baseline_is_clipped_to_it(self, tmp_path: Path) -> None:
        """A backlog line running to zero on the left is a lie a reader cannot detect."""
        store = build_project(tmp_path / "young", "young")
        plant_task(store, "task-001", created=NOW - timedelta(days=9))

        payload = AnalyticsProjection(store.read_connection(), "young", now=NOW).build("90d")

        assert len(payload["backlog"]) == 10  # nine days plus today, not ninety
        assert payload["backlog"][0]["open_count"] == 1

    def test_a_bucket_holding_a_reconstructed_event_is_marked_and_its_neighbours_are_not(
        self, tmp_path: Path
    ) -> None:
        """§7.3: ``estimated`` is per bucket, not per response.

        A response-level flag would make the page hatch a prefix whose length it guessed,
        which is the failure the per-bucket field exists to prevent.
        """
        store = build_project(tmp_path / "mixed", "mixed")
        plant_task(store, "task-001", created=NOW - timedelta(days=5), source="backfilled")
        plant_task(store, "task-002", created=NOW - timedelta(days=3), source="native")

        payload = AnalyticsProjection(store.read_connection(), "mixed", now=NOW).build("30d")
        marked = {point["day"] for point in payload["backlog"] if point["estimated"]}

        assert marked == {local_day(NOW - timedelta(days=5), CHICAGO)}

    def test_nothing_is_marked_estimated_when_every_event_is_native(self, served) -> None:
        client, _store, _manager = served
        assert not any(point["estimated"] for point in analytics(client)["backlog"])

    @pytest.mark.parametrize(
        "key, bucket, throughput_bucket",
        [("30d", "day", "week"), ("90d", "day", "week"), ("12m", "week", "month")],
    )
    def test_the_grain_follows_the_range(
        self, served, key: str, bucket: str, throughput_bucket: str
    ) -> None:
        """§8.3 fixes the spine's grain; §8.4's percentiles fix throughput's.

        Both are on the response so the page never has to hold a second copy of the rule.
        """
        client, _store, _manager = served

        window = analytics(client, range=key)["range"]

        assert (window["bucket"], window["throughput_bucket"]) == (bucket, throughput_bucket)

    def test_the_level_carries_forward_across_days_with_no_event(self, served) -> None:
        """The point of a spine: a quiet day reports the level, not a gap or a zero."""
        client, _store, _manager = served

        payload = analytics(client, range="90d")
        quiet = [
            point for point in payload["backlog"] if point["opened"] == 0 and point["closed"] == 0
        ]

        assert quiet, "the fixture should contain days with no backlog event"
        assert all(point["open_count"] > 0 for point in quiet)


class TestReconciliation:
    """ac-4 -- the two assertions §5.3 and §6.1 item D name as dependencies."""

    def test_the_totals_equal_the_dashboards_stats_field_for_field(self, served) -> None:
        """Two implementations of six rules, held against each other.

        ``agentjobs.analytics`` states them as SQL over ``ix_task_counts``;
        ``agentjobs.dashboard`` states them as Python over the assembled task list. The
        endpoint deliberately does not call the latter -- see ``AnalyticsProjection.totals``
        -- so this comparison has something to say.
        """
        client, _store, manager = served

        totals = analytics(client)["totals"]
        stats = build_dashboard_snapshot(manager)["stats"]

        assert {key: totals[key] for key in stats} == stats

    def test_open_is_the_number_the_backlog_series_ends_on(self, served) -> None:
        client, _store, _manager = served

        payload = analytics(client, range="all")

        assert payload["backlog"][-1]["open_count"] == payload["totals"]["open"]

    def test_the_history_invariant_holds_through_the_read_path(self, served) -> None:
        """``SUM(open_delta)`` equals the open-task count (§3.2).

        Asserted here against what a client is handed, not only against the store: the
        page is the thing that goes visibly wrong when the history stops adding up, and
        a reader looking at a level that disagrees with the board has no way to tell
        which of the two is lying.
        """
        client, store, _manager = served

        summed, counted = store.open_delta_reconciles()
        payload = analytics(client, range="all")

        assert summed == counted == payload["totals"]["open"]

    def test_the_holder_bands_sum_to_the_open_level(self, served) -> None:
        """Every open task is held by exactly one of agent, human or external."""
        client, _store, _manager = served

        payload = analytics(client, range="all")
        last = payload["holders"][-1]

        assert last["agent"] + last["human"] + last["external"] == payload["totals"]["open"]

    def test_an_archived_open_task_counts_in_the_level_and_in_nothing_else(self, served) -> None:
        """§3.4: the level ignores ``archived``; the attention panels exclude it."""
        client, _store, _manager = served

        payload = analytics(client, range="all")
        aged = sum(bucket["tasks"] for bucket in payload["aging"])

        assert payload["totals"]["open"] == aged + 1
        assert "task-011" not in {task["task_id"] for task in payload["oldest"]}

    def test_completed_is_not_closed(self, served) -> None:
        """§3.3: reporting closed as completed would overstate delivered work."""
        client, _store, _manager = served

        payload = analytics(client, range="all")
        completed = sum(point["tasks_completed"] for point in payload["throughput"])
        cancelled = sum(point["cancelled"] for point in payload["throughput"])

        assert (completed, cancelled) == (4, 1)
        assert payload["totals"]["completed"] == 4


class TestThroughput:
    """§3.3 and §8.4 -- what the bars plot and what sits beside them."""

    def test_unique_tasks_and_completion_events_are_kept_apart(self, tmp_path: Path) -> None:
        """A reopened task closes twice; the chart plots the honest number.

        Both are returned so a reader who notices the sum not matching has an answer
        rather than a suspicion (§3.3).
        """
        store = build_project(tmp_path / "reopened", "reopened")
        created = NOW - timedelta(days=20)
        plant_task(store, "task-001", created=created, closed=NOW - timedelta(days=10))
        with store.database.write() as connection:
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_from, lifecycle_to, outcome_from, outcome_to)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "reopened",
                    "task-001",
                    iso(NOW - timedelta(days=9)),
                    "claude",
                    "reopen",
                    "native",
                    "closed",
                    "ready",
                    "completed",
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_from, lifecycle_to, outcome_to) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "reopened",
                    "task-001",
                    iso(NOW - timedelta(days=8)),
                    "claude",
                    "close",
                    "native",
                    "ready",
                    "closed",
                    "completed",
                ),
            )

        payload = AnalyticsProjection(store.read_connection(), "reopened", now=NOW).build("90d")
        completed = sum(point["tasks_completed"] for point in payload["throughput"])
        events = sum(point["completion_events"] for point in payload["throughput"])

        assert (completed, events) == (1, 2)

    def test_a_rewrite_of_a_closed_record_is_not_a_second_close(self, tmp_path: Path) -> None:
        """§3.2 property 2: only a *transition* counts.

        An event whose ``lifecycle_from`` is already ``closed`` is an edit to a closed
        task -- a reparenting, say -- and counting it would inflate throughput by however
        many times the record was touched afterwards.
        """
        store = build_project(tmp_path / "rewritten", "rewritten")
        plant_task(
            store, "task-001", created=NOW - timedelta(days=20), closed=NOW - timedelta(days=10)
        )
        with store.database.write() as connection:
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, ts, actor, kind, source,"
                " lifecycle_from, lifecycle_to, outcome_from, outcome_to, parent_to)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "rewritten",
                    "task-001",
                    iso(NOW - timedelta(days=5)),
                    "claude",
                    "update_content",
                    "native",
                    "closed",
                    "closed",
                    "completed",
                    "completed",
                    "task-999",
                ),
            )

        payload = AnalyticsProjection(store.read_connection(), "rewritten", now=NOW).build("90d")

        assert sum(point["completion_events"] for point in payload["throughput"]) == 1

    def test_a_bucket_with_nothing_in_it_reports_null_percentiles_not_zero(self, served) -> None:
        """§7.3: ``null`` is the honest value for a percentile with no sample."""
        client, _store, _manager = served

        empty = [
            point for point in analytics(client, range="90d")["throughput"] if point["sample"] == 0
        ]

        assert empty, "the fixture should contain a week with no completions"
        assert all(point["cycle_p50_days"] is None for point in empty)

    def test_sample_is_returned_beside_the_percentiles(self, served) -> None:
        """So the page can suppress a line drawn from three points (§8.4)."""
        client, _store, _manager = served

        for point in analytics(client, range="all")["throughput"]:
            assert (point["sample"] > 0) == (point["cycle_p50_days"] is not None)

    @pytest.mark.parametrize(
        "values, fraction, expected",
        [([], 0.5, None), ([4.0], 0.9, 4.0), ([1.0, 2.0, 3.0], 0.5, 2.0)],
    )
    def test_the_percentile_helper(self, values, fraction, expected) -> None:
        assert percentile(values, fraction) == expected


class TestAgingAndStuck:
    """§8.5 and §8.6 -- the two panels that are calls to attention."""

    def test_every_age_band_is_emitted_whether_or_not_it_holds_anything(self, served) -> None:
        """A missing bar reads as a rendering fault; a bar labelled zero reads as a fact."""
        client, _store, _manager = served

        assert [bucket["label"] for bucket in analytics(client)["aging"]] == [
            "0-6d",
            "7-29d",
            "30-89d",
            "90d+",
        ]

    def test_the_bands_partition_the_open_unarchived_work(self, served) -> None:
        client, _store, _manager = served

        payload = analytics(client)
        counts = {bucket["label"]: bucket["tasks"] for bucket in payload["aging"]}

        assert counts == {"0-6d": 1, "7-29d": 1, "30-89d": 2, "90d+": 1}

    def test_the_oldest_list_is_oldest_first_and_excludes_the_archived(self, served) -> None:
        client, _store, _manager = served

        oldest = analytics(client)["oldest"]

        assert [task["task_id"] for task in oldest] == [
            "task-009",
            "task-010",
            "task-008",
            "task-007",
            "task-006",
        ]
        assert oldest[0]["age_days"] == pytest.approx(120, abs=0.1)

    def test_days_held_is_measured_from_the_last_move_of_the_ball(self, served) -> None:
        """Not from the last activity of any kind.

        A comment on a task parked for five weeks does not restart the clock on the
        parking, and a panel that said it did would under-report the thing it exists to
        surface. ``task-009`` was created 120 days ago and handed to a person 100 days
        ago; the panel's answer is 100.
        """
        client, _store, _manager = served

        stuck = {
            (group["ball"], group["ball_reason"]): group for group in analytics(client)["stuck"]
        }

        assert stuck[("human", "spec")]["max_days_held"] == pytest.approx(100, abs=0.1)
        assert stuck[("human", "spec")]["oldest_task_id"] == "task-009"

    def test_groups_are_ordered_by_how_many_tasks_are_in_them(self, served) -> None:
        client, _store, _manager = served

        counts = [group["tasks"] for group in analytics(client)["stuck"]]

        assert counts == sorted(counts, reverse=True)

    def test_the_groups_cover_every_open_unarchived_task_once(self, served) -> None:
        client, _store, _manager = served

        payload = analytics(client)
        grouped = sum(group["tasks"] for group in payload["stuck"])

        assert grouped == sum(bucket["tasks"] for bucket in payload["aging"])


class TestCoverage:
    """§3.6 and §9 -- what the page is allowed to claim, as data rather than prose."""

    def test_a_project_with_no_history_is_two_hundred_with_a_null_baseline(self, served) -> None:
        """ac-5, and §7.4: an empty project is a state, not an error."""
        client, _store, _manager = served

        payload = analytics(client, "fresh")

        assert payload["coverage"]["baseline_at"] is None
        assert payload["coverage"]["baseline_kind"] == "unknown"
        assert payload["coverage"]["complete"] is False
        assert payload["backlog"] == []
        assert payload["holders"] == []
        assert payload["throughput"] == []

    def test_an_empty_project_is_distinguishable_from_one_whose_counts_are_zero(
        self, tmp_path: Path
    ) -> None:
        """ac-5's second half, and the whole of §9.1 against §9.2.

        "Nothing has happened here" and "we know what happened and it was nothing" are
        different sentences, and the page renders them differently. A client tells them
        apart by ``coverage.baseline_at``: null means no claim at all; a date beside a
        series of honest zeros means the store watched and nothing came.
        """
        empty = build_project(tmp_path / "empty", "empty")
        quiet = build_project(tmp_path / "quiet", "quiet")
        plant_task(
            quiet, "task-001", created=NOW - timedelta(days=40), closed=NOW - timedelta(days=39)
        )

        no_history = AnalyticsProjection(empty.read_connection(), "empty", now=NOW).build("30d")
        genuine_zero = AnalyticsProjection(quiet.read_connection(), "quiet", now=NOW).build("30d")

        assert no_history["coverage"]["baseline_at"] is None
        assert no_history["backlog"] == []
        assert genuine_zero["coverage"]["baseline_at"] is not None
        assert genuine_zero["backlog"], "a watched window reports its zeros"
        assert {point["open_count"] for point in genuine_zero["backlog"]} == {0}

    def test_a_window_inside_the_native_span_is_complete(self, tmp_path: Path) -> None:
        store = build_project(tmp_path / "native", "native")
        plant_task(store, "task-001", created=NOW - timedelta(days=60))

        payload = AnalyticsProjection(store.read_connection(), "native", now=NOW).build("30d")

        assert payload["coverage"]["complete"] is True
        assert payload["coverage"]["note"] == "History from 20 Jul 2026."

    def test_a_window_reaching_before_the_native_span_is_not_complete_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """§9.3: the reader is told once, on the page, not in a document nobody reads."""
        store = build_project(tmp_path / "mixed", "mixed")
        plant_task(store, "task-001", created=NOW - timedelta(days=60), source="backfilled")
        plant_task(store, "task-002", created=NOW - timedelta(days=5), source="native")

        payload = AnalyticsProjection(store.read_connection(), "mixed", now=NOW).build("90d")

        assert payload["coverage"]["complete"] is False
        assert payload["coverage"]["baseline_kind"] == "reconstructed"
        assert "bounds rather than observations" in payload["coverage"]["note"]

    def test_the_event_counts_are_broken_out_by_source(self, tmp_path: Path) -> None:
        store = build_project(tmp_path / "sources", "sources")
        plant_task(store, "task-001", created=NOW - timedelta(days=60), source="backfilled")
        plant_task(store, "task-002", created=NOW - timedelta(days=5), source="native")

        payload = AnalyticsProjection(store.read_connection(), "sources", now=NOW).build("all")

        assert payload["coverage"]["events"] == {"backfilled": 1, "native": 1}


class TestQueryPlans:
    """ac-6 -- every query is an indexed scan, and none reads a task document.

    Asserted on the **plan** rather than on wall-clock time, for the reason
    ``test_sqlstore_analytics`` gives: a threshold in milliseconds means something
    different on every machine, while a plan says whether the index is being used, which
    is what decides whether this page is viable. The plans this records are on the task
    (ac-6) as well as here.
    """

    @pytest.fixture()
    def store(self, tmp_path: Path) -> SqlTaskStore:
        store = build_project(tmp_path / "plans", "plans")
        seed_history(store)
        return store

    @pytest.mark.parametrize("name", sorted(QUERIES))
    def test_the_query_is_answered_by_an_index(self, store: SqlTaskStore, name: str) -> None:
        sql = QUERIES[name]
        rows = (
            store.read_connection()
            .execute("EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?"))))
            .fetchall()
        )

        steps = [row["detail"] for row in rows]
        assert steps
        for step in steps:
            assert (
                "USING INDEX" in step
                or "USING COVERING INDEX" in step
                or (step.startswith("BLOOM FILTER"))
            ), f"{name}: {step}"

    @pytest.mark.parametrize("name", sorted(QUERIES))
    def test_the_query_touches_no_json_column(self, name: str) -> None:
        """§3.1 and the task's own constraint. A JSON column is a task document again.

        Checked on the text rather than on the plan because that is where it would be
        introduced: ``json_extract`` over ``spec_context_json`` would plan perfectly well
        and reintroduce exactly the cost this page was built to avoid.
        """
        sql = QUERIES[name].lower()

        assert "json" not in sql
        assert "_json" not in sql

    def test_the_whole_payload_is_one_bounded_set_of_reads(self, store: SqlTaskStore) -> None:
        """A guard on the shape of the read, not on its speed.

        Twelve statements is what §7.1's "one request for the whole page" costs. A
        regression that put a query inside a loop -- one per task, one per bucket --
        would not change any plan above and would change this.
        """
        connection = store.read_connection()
        executed: List[str] = []
        connection.set_trace_callback(executed.append)
        try:
            AnalyticsProjection(connection, "plans", now=NOW).build("all")
        finally:
            connection.set_trace_callback(None)

        assert len(executed) <= 15, executed


class TestSpineHelpers:
    """The bucket arithmetic, directly, where a failure names the rule it broke."""

    @pytest.mark.parametrize(
        "day, bucket, expected",
        [
            (date(2026, 9, 18), "day", date(2026, 9, 18)),
            (date(2026, 9, 18), "week", date(2026, 9, 14)),  # the Monday
            (date(2026, 9, 14), "week", date(2026, 9, 14)),
            (date(2026, 9, 18), "month", date(2026, 9, 1)),
        ],
    )
    def test_a_day_falls_into_its_bucket(self, day, bucket, expected) -> None:
        assert bucket_start(day, bucket) == expected

    def test_a_month_spine_crosses_a_year_boundary(self) -> None:
        assert spine(date(2025, 11, 20), date(2026, 2, 3), "month") == [
            date(2025, 11, 1),
            date(2025, 12, 1),
            date(2026, 1, 1),
            date(2026, 2, 1),
        ]

    def test_a_day_spine_has_no_gaps(self) -> None:
        days = spine(date(2026, 2, 26), date(2026, 3, 2), "day")
        assert days == [
            date(2026, 2, 26),
            date(2026, 2, 27),
            date(2026, 2, 28),
            date(2026, 3, 1),
            date(2026, 3, 2),
        ]
