"""The analytics API's second set -- ``docs/analytics-design.md`` sections 17, 18 and 21.

One corpus, planted row by row at instants relative to a fixed clock, holding every edge
case section 17.4 decided by name: a task with no review handoff, one with two review
round-trips, a reopen, two claims with a release between them, a close by an ``import``
row, a draft promoted late, and an approve-and-close. Each exists to make one assertion
possible, and the expected numbers below are counted from :func:`seed_process` rather
than from a run.

Two things this file holds that the first set's file does not:

*   **Section 17.5's invariant, per task.** ``queue + work + waiting + review + finish``
    equals the task's total to the second, for every task the projection emits. It is
    the one check that catches a boundary rule quietly changed.
*   **Section 21.1's coverage, per series.** A series whose source is younger than the
    range starts where the source starts and says so; it never draws zero over history
    nobody recorded. Asserted on the payload, not on a page.

The projection tests inject ``NOW``; the HTTP tests read the wall clock the endpoint
reads and assert on relative values, which is task-464's lesson restated.
"""

from __future__ import annotations


import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.analytics import (
    EXECUTION_QUERIES,
    QUERIES,
    SEGMENTS,
    UNINDEXED_QUERIES,
    AnalyticsProjection,
    Window,
    bucket_start,
    local_day,
    resolve_zone,
)
from agentjobs.api import models as api_models
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.execution.factory import close_execution_stores, execution_store_for
from agentjobs.execution.store import ExecutionStore
from agentjobs.projects import ProjectRegistry
from agentjobs.sqlstore import SqlTaskStore
from support import task_store

NOW = datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc)
PROJECT = "proc"

DAY = 86400.0
HOUR = 3600.0


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_project(root: Path, project_id: str) -> SqlTaskStore:
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": project_id, "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    store = task_store(root / "tasks", project_id=project_id)
    with store.database.write() as connection:
        connection.execute(
            "UPDATE project SET reporting_tz = ? WHERE project_id = ?",
            ("America/Chicago", project_id),
        )
    return store


# ---------------------------------------------------------------------------
# Planting rows
# ---------------------------------------------------------------------------

EVENT_COLUMNS = (
    "ts",
    "kind",
    "source",
    "lifecycle_from",
    "lifecycle_to",
    "ball_from",
    "ball_reason_from",
    "ball_to",
    "ball_reason_to",
    "outcome_to",
)


def plant(
    store: SqlTaskStore,
    task_id: str,
    events: Sequence[Dict[str, Any]],
    *,
    lifecycle: str,
    ball: Optional[str] = None,
    ball_reason: Optional[str] = None,
    outcome: Optional[str] = None,
    closed_at: Optional[datetime] = None,
    position: Optional[int] = None,
) -> None:
    """One task row and its history, exactly as given.

    Straight SQL, like ``test_analytics_api.plant_task``: these cases are about the
    shape of the history the fold sees, and the rows still satisfy every ``CHECK`` in
    ``001_initial.sql``.
    """
    created = events[0]["ts"]
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO task(project_id, task_id, title, created_at, updated_at,"
            " lifecycle, ball, ball_reason, ball_prompt, outcome, archived, priority,"
            " queue_position, category, spec_summary, spec_description, closed_at,"
            " last_activity_at, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                task_id,
                f"Title of {task_id}",
                iso(created),
                iso(closed_at or created),
                lifecycle,
                ball,
                ball_reason,
                None if ball_reason in (None, "available") else "Do the thing.",
                outcome,
                0,
                "medium",
                position,
                "engineering",
                f"Summary of {task_id}.",
                f"Description of {task_id}.",
                iso(closed_at) if closed_at else None,
                iso(closed_at or created),
                "claude" if lifecycle == "active" else None,
            ),
        )
        for event in events:
            values = {column: event.get(column) for column in EVENT_COLUMNS}
            values["ts"] = iso(event["ts"])
            values["source"] = values["source"] or "native"
            connection.execute(
                "INSERT INTO task_event(project_id, task_id, actor, "
                + ", ".join(EVENT_COLUMNS)
                + ") VALUES (?, ?, ?, "
                + ", ".join("?" for _ in EVENT_COLUMNS)
                + ")",
                (store.project_id, task_id, "claude", *[values[c] for c in EVENT_COLUMNS]),
            )


def ev(
    ts: datetime,
    kind: str,
    *,
    lf: Optional[str] = None,
    lt: Optional[str] = None,
    bf: Optional[str] = None,
    rf: Optional[str] = None,
    bt: Optional[str] = None,
    rt: Optional[str] = None,
    outcome: Optional[str] = None,
    source: str = "native",
) -> Dict[str, Any]:
    return {
        "ts": ts,
        "kind": kind,
        "source": source,
        "lifecycle_from": lf,
        "lifecycle_to": lt,
        "ball_from": bf,
        "ball_reason_from": rf,
        "ball_to": bt,
        "ball_reason_to": rt,
        "outcome_to": outcome,
    }


def plant_run(
    store: SqlTaskStore,
    run_id: str,
    task_id: str,
    started: datetime,
    *,
    ended: Optional[datetime] = None,
    outcome: Optional[str] = None,
    trigger: str = "manual",
    seconds: Optional[float] = None,
) -> None:
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO task_run(project_id, run_id, task_id, agent, runner, mode, merge_mode,"
            " trigger, caused_by, git_head, cwd, argv_json, started_at, ended_at, outcome,"
            " duration_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                run_id,
                task_id,
                "claude",
                "claude-cli",
                "session",
                "review",
                trigger,
                1,
                "abc1234",
                "C:/somewhere",
                "[]",
                iso(started),
                iso(ended) if ended else None,
                outcome,
                seconds,
            ),
        )


def plant_log(
    store: SqlTaskStore,
    task_id: str,
    entry_id: int,
    ts: datetime,
    entry_type: str,
    *,
    re_entry: Optional[int] = None,
) -> None:
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO log_entry(project_id, task_id, entry_id, ts, actor, type, body, re)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (store.project_id, task_id, entry_id, iso(ts), "claude", entry_type, "…", re_entry),
        )


def seed_process(store: SqlTaskStore, reference: datetime) -> None:
    """Section 17.4's cases, one task each, closed inside the last thirty days.

    ``reference`` is the clock the reader will measure from. Durations are chosen so
    every expected value below is a whole number of hours.
    """
    day = lambda n: reference - timedelta(days=n)  # noqa: E731 - read once
    hours = lambda n: reference - timedelta(hours=n)  # noqa: E731

    # task-101 -- no review handoff: claim, work, close by the finisher. Its finish
    # segment comes from the merged finish row planted below (section 17.3).
    plant(
        store,
        "task-101",
        [
            ev(day(20), "create", lt="ready", bt="agent", rt="available"),
            ev(
                day(19),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                day(18),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(18),
    )
    # task-102 -- two review round-trips, then an approval and a finish.
    plant(
        store,
        "task-102",
        [
            ev(day(15), "create", lt="ready", bt="agent", rt="available"),
            ev(
                day(14),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                day(13),
                "handoff",
                lf="active",
                lt="active",
                bf="agent",
                rf="work",
                bt="human",
                rt="review",
            ),
            ev(
                hours(12 * 25),
                "handoff",
                lf="active",
                lt="active",
                bf="human",
                rf="review",
                bt="agent",
                rt="revise",
            ),
            ev(
                day(12),
                "handoff",
                lf="active",
                lt="active",
                bf="agent",
                rf="revise",
                bt="human",
                rt="review",
            ),
            ev(
                day(11),
                "handoff",
                lf="active",
                lt="active",
                bf="human",
                rf="review",
                bt="agent",
                rt="work",
            ),
            ev(
                day(11) + timedelta(hours=1),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(11) + timedelta(hours=1),
    )
    # task-103 -- closed, reopened three days later, worked a day, closed again. The
    # closed interval is in no segment and not in the total.
    plant(
        store,
        "task-103",
        [
            ev(day(10), "create", lt="ready", bt="agent", rt="available"),
            ev(
                hours(9 * 24 + 12),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                day(9),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
            ev(day(6), "reopen", lf="closed", lt="active", bt="agent", rt="work"),
            ev(
                day(5),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(5),
    )
    # task-104 -- claimed, released, claimed again. Its creation is reconstructed, so
    # its bucket is estimated.
    plant(
        store,
        "task-104",
        [
            ev(day(8), "create", lt="ready", bt="agent", rt="available", source="reconstructed"),
            ev(
                day(7),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                hours(6 * 24 + 12),
                "release",
                lf="active",
                lt="ready",
                bf="agent",
                rf="work",
                bt="agent",
                rt="available",
            ),
            ev(
                day(6),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                hours(5 * 24 + 12),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=hours(5 * 24 + 12),
    )
    # task-105 -- its last close is an import reconciliation row: the close time is
    # unknown, so it is excluded from every segment sample (section 17.4).
    plant(
        store,
        "task-105",
        [
            ev(day(30), "create", lt="ready", bt="agent", rt="available", source="reconstructed"),
            ev(
                day(29),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
                source="reconstructed",
            ),
            ev(
                day(4),
                "import",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
                source="reconstructed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(4),
    )
    # task-106 -- born a draft (waiting), promoted a day later (queue), then a wait on
    # a decision in the middle of the work.
    plant(
        store,
        "task-106",
        [
            ev(day(12), "create", lt="draft", bt="human", rt="spec"),
            ev(
                day(11),
                "handoff",
                lf="draft",
                lt="ready",
                bf="human",
                rf="spec",
                bt="agent",
                rt="available",
            ),
            ev(
                hours(10 * 24 + 12),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                day(10),
                "handoff",
                lf="active",
                lt="active",
                bf="agent",
                rf="work",
                bt="human",
                rt="decision",
            ),
            ev(
                hours(9 * 24 + 12),
                "handoff",
                lf="active",
                lt="active",
                bf="human",
                rf="decision",
                bt="agent",
                rt="work",
            ),
            ev(
                day(9),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(9),
    )
    # task-107 -- approve-and-close in one act: the close leaves review.
    plant(
        store,
        "task-107",
        [
            ev(day(3), "create", lt="ready", bt="agent", rt="available"),
            ev(
                hours(2 * 24 + 22),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                hours(2 * 24 + 12),
                "handoff",
                lf="active",
                lt="active",
                bf="agent",
                rf="work",
                bt="human",
                rt="review",
            ),
            ev(
                day(2),
                "close",
                lf="active",
                lt="closed",
                bf="human",
                rf="review",
                outcome="completed",
            ),
        ],
        lifecycle="closed",
        outcome="completed",
        closed_at=day(2),
    )
    # task-108 -- open and in review now, with an unanswered question on it.
    plant(
        store,
        "task-108",
        [
            ev(day(3), "create", lt="ready", bt="agent", rt="available"),
            ev(
                day(2),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                hours(6),
                "handoff",
                lf="active",
                lt="active",
                bf="agent",
                rf="work",
                bt="human",
                rt="review",
            ),
        ],
        lifecycle="active",
        ball="human",
        ball_reason="review",
        position=100,
    )
    # task-109 -- cancelled: on the throughput chart, in no segment sample.
    plant(
        store,
        "task-109",
        [
            ev(day(3), "create", lt="ready", bt="agent", rt="available"),
            ev(
                day(1),
                "close",
                lf="ready",
                lt="closed",
                bf="agent",
                rf="available",
                outcome="cancelled",
            ),
        ],
        lifecycle="closed",
        outcome="cancelled",
        closed_at=day(1),
    )

    # Finishes. fin_a closed task-101 (unreviewed): merged, finished 5 s after the
    # close, 300 s long. fin_b escalated at the gate on task-102, imported. fin_c is
    # task-102's approved finish, which waited 30 s for the runway.
    store.record_finish(
        "fin_a",
        {
            "task_id": "task-101",
            "started_at": iso(day(18) - timedelta(seconds=295)),
            "finished_at": iso(day(18) + timedelta(seconds=5)),
            "seconds": 300.0,
            "outcome": "finished",
            "merged": True,
            "merge_commit": "abc",
            "source": "native",
        },
        [
            {"seq": 1, "step": "preflight", "ok": True, "seconds": 1.0, "ts": iso(day(18))},
            {"seq": 2, "step": "runway", "ok": True, "seconds": 0.0, "ts": iso(day(18))},
            {"seq": 3, "step": "gate", "ok": True, "seconds": 250.0, "ts": iso(day(18))},
            {"seq": 4, "step": "merge", "ok": True, "seconds": 2.0, "ts": iso(day(18))},
        ],
    )
    store.record_finish(
        "fin_b",
        {
            "task_id": "task-102",
            "started_at": iso(day(13)),
            "finished_at": iso(day(13) + timedelta(seconds=200)),
            "seconds": 200.0,
            "outcome": "escalated",
            "reason": "gate_failed",
            "stopped_at": "gate",
            "source": "imported",
        },
        [
            {"seq": 1, "step": "preflight", "ok": True, "seconds": 1.0, "ts": iso(day(13))},
            {"seq": 2, "step": "gate", "ok": False, "seconds": 190.0, "ts": iso(day(13))},
        ],
    )
    store.record_finish(
        "fin_c",
        {
            "task_id": "task-102",
            "started_at": iso(day(11)),
            "finished_at": iso(day(11) + timedelta(seconds=600)),
            "seconds": 600.0,
            "outcome": "finished",
            "merged": True,
            "merge_commit": "def",
            "source": "native",
        },
        [
            {"seq": 1, "step": "preflight", "ok": True, "seconds": 1.0, "ts": iso(day(11))},
            {"seq": 2, "step": "runway", "ok": True, "seconds": 30.0, "ts": iso(day(11))},
            {"seq": 3, "step": "gate", "ok": True, "seconds": 500.0, "ts": iso(day(11))},
            {
                "seq": 4,
                "step": "verify",
                "ok": True,
                "skipped": True,
                "seconds": 0.0,
                "ts": iso(day(11)),
            },
        ],
    )

    # Gates. g1: the finisher's, green, imported. g2: a run's, red at pytest. g3: a
    # run's, green. g4: partial, and therefore in no gate series.
    store.record_gate_run(
        "fin_a:g1",
        {
            "origin": "finish",
            "finish_id": "fin_a",
            "task_id": "task-101",
            "scope": "full",
            "started_at": iso(day(18) - timedelta(seconds=290)),
            "finished_at": iso(day(18) - timedelta(seconds=50)),
            "seconds": 240.0,
            "passed": True,
            "source": "imported",
        },
        [
            {
                "seq": 1,
                "stage": "pytest",
                "seconds": 120.0,
                "passed": True,
                "started_at": iso(day(18)),
            },
            {
                "seq": 2,
                "stage": "e2e",
                "seconds": 100.0,
                "passed": True,
                "started_at": iso(day(18)),
            },
        ],
    )
    store.record_gate_run(
        "run_2:g1",
        {
            "origin": "run",
            "run_id": "run_2",
            "task_id": "task-102",
            "scope": "full",
            "started_at": iso(day(13) + timedelta(hours=1)),
            "finished_at": iso(day(13) + timedelta(hours=1, seconds=60)),
            "seconds": 60.0,
            "passed": False,
            "failed_stage": "pytest",
            "source": "native",
        },
        [
            {
                "seq": 1,
                "stage": "pytest",
                "seconds": None,
                "passed": False,
                "started_at": iso(day(13)),
            }
        ],
    )
    store.record_gate_run(
        "run_3:g1",
        {
            "origin": "run",
            "run_id": "run_3",
            "task_id": "task-102",
            "scope": "full",
            "started_at": iso(day(12) - timedelta(hours=1)),
            "finished_at": iso(day(12) - timedelta(hours=1) + timedelta(seconds=300)),
            "seconds": 300.0,
            "passed": True,
            "source": "native",
        },
        [
            {
                "seq": 1,
                "stage": "pytest",
                "seconds": 180.0,
                "passed": True,
                "started_at": iso(day(12)),
            },
            {"seq": 2, "stage": "e2e", "seconds": 90.0, "passed": True, "started_at": iso(day(12))},
        ],
    )
    store.record_gate_run(
        "man_1",
        {
            "origin": "manual",
            "task_id": "task-102",
            "scope": "partial",
            "started_at": iso(day(12)),
            "seconds": 10.0,
            "passed": True,
            "source": "native",
        },
        [],
    )

    # Runs. task-101 took one; task-102 took two, one of them interrupted; task-108
    # has one in the air.
    plant_run(store, "run_1", "task-101", day(19), ended=day(18), outcome="completed", seconds=HOUR)
    plant_run(
        store,
        "run_2",
        "task-102",
        day(14),
        ended=day(13),
        outcome="completed",
        trigger="child",
        seconds=2 * HOUR,
    )
    plant_run(
        store,
        "run_3",
        "task-102",
        hours(12 * 25),
        ended=day(12),
        outcome="interrupted",
        trigger="auto",
        seconds=0.5 * HOUR,
    )
    plant_run(store, "run_4", "task-108", day(2), trigger="manual")

    # Questions. One answered in 2 hours on task-102; one unanswered on the open
    # task-108; one unanswered on the closed task-101.
    plant_log(store, "task-102", 1, day(13) + timedelta(hours=6), "question")
    plant_log(store, "task-102", 2, day(13) + timedelta(hours=8), "answer", re_entry=1)
    plant_log(store, "task-102", 3, day(13) + timedelta(hours=8), "handoff", re_entry=1)
    plant_log(store, "task-108", 1, day(2), "question")
    plant_log(store, "task-101", 1, day(19) + timedelta(hours=12), "question")


def seed_journal(store: ExecutionStore, reference: datetime) -> None:
    """Two admissions, one queued dispatch and one usage-limit pause for this project."""
    day = lambda n: reference - timedelta(days=n)  # noqa: E731
    with store.transaction("seed") as connection:
        for run_id, admitted, launched in (
            ("run_1", day(19), day(19) + timedelta(seconds=2)),
            ("run_2", day(14), day(14) + timedelta(seconds=4)),
        ):
            connection.execute(
                "INSERT INTO run_attempt(run_id, project_id, task_id, takes_slot, holder,"
                " state, admitted_at, launched_at, concluded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    PROJECT,
                    "task-1",
                    1,
                    "test",
                    "terminal",
                    iso(admitted),
                    iso(launched),
                    iso(launched),
                ),
            )
        connection.execute(
            "INSERT INTO run_attempt(run_id, project_id, task_id, takes_slot, holder,"
            " state, admitted_at) VALUES (?,?,?,?,?,?,?)",
            ("run_other", "other", "task-1", 1, "test", "terminal", iso(day(10))),
        )
        connection.execute(
            "INSERT INTO dispatch_queue(queue_id, seq, project_id, task_id, status, run_id,"
            " queued_at, claimed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "q1",
                1,
                PROJECT,
                "task-1",
                "started",
                "run_2",
                iso(day(14) - timedelta(seconds=90)),
                iso(day(14)),
                iso(day(14)),
            ),
        )
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, state,"
            " opened_at, next_probe_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("inc_1", "usage_limit", "p", "{}", "recovered", iso(day(9)), iso(day(9)), iso(day(9))),
        )
        for run_id, stall, recovered in (
            ("run_a", day(9), day(9) + timedelta(hours=1)),
            ("run_b", day(9), day(9) + timedelta(minutes=30)),
        ):
            connection.execute(
                "INSERT INTO auth_waiter(incident_id, run_id, project_id, task_id, session_id,"
                " stall_at, status, joined_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "inc_1",
                    run_id,
                    PROJECT,
                    "task-1",
                    "s",
                    iso(stall),
                    "recovered",
                    iso(stall),
                    iso(recovered),
                ),
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def corpus(tmp_path: Path) -> Iterator[Tuple[AnalyticsProjection, SqlTaskStore]]:
    """The seeded store and a projection over it on the fixed clock."""
    store = build_project(tmp_path / PROJECT, PROJECT)
    seed_process(store, NOW)
    journal = execution_store_for(tmp_path / "home")
    seed_journal(journal, NOW)
    projection = AnalyticsProjection(store.read_connection(), PROJECT, now=NOW, execution=journal)
    try:
        yield projection, store
    finally:
        close_execution_stores()


def window_for(projection: AnalyticsProjection, key: str) -> Window:
    zone = resolve_zone(projection.project_settings()[0])
    return projection.window(key, projection.coverage(zone), zone)


def week_of(moment: datetime) -> date:
    return bucket_start(local_day(moment, resolve_zone("America/Chicago")), "week")


def point_at(points: List[Dict[str, Any]], bucket: date) -> Dict[str, Any]:
    matches = [point for point in points if point["bucket"] == bucket]
    assert len(matches) == 1, f"no single point for {bucket}: {[p['bucket'] for p in points]}"
    return matches[0]


@pytest.fixture()
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, SqlTaskStore, datetime]]:
    """One server over the process corpus, seeded from the clock the endpoint reads."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    reference = datetime.now(timezone.utc)
    store = build_project(tmp_path / PROJECT, PROJECT)
    seed_process(store, reference)
    seed_journal(execution_store_for(tmp_path / "home"), reference)
    build_project(tmp_path / "empty", "empty")

    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / PROJECT, project_id=PROJECT)
    registry.add(tmp_path / "empty", project_id="empty")

    with TestClient(app) as client:
        yield client, store, reference

    close_execution_stores()
    reset_dependency_cache()


# ---------------------------------------------------------------------------
# Section 17: the segments
# ---------------------------------------------------------------------------


class TestSegments:
    def test_the_five_segments_sum_to_the_total_on_every_task(self, corpus) -> None:
        """Section 17.5's invariant, held per task rather than per percentile."""
        projection, _store = corpus
        tasks = projection.segment_tasks(window_for(projection, "all"))

        assert len(tasks) == 7
        for task in tasks.values():
            assert task.closed_at is not None
            assert abs(sum(task.seconds.values()) - task.total) < 1.0, task
            assert all(value >= 0 for value in task.seconds.values()), task

    def test_no_review_handoff_takes_its_finish_from_the_finish_table(self, corpus) -> None:
        """Section 17.3: review is zero, and the merged row's 300 s is carved from work."""
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-101"]

        assert task.unreviewed
        assert task.approvals == 0
        assert task.seconds["queue"] == pytest.approx(DAY)
        assert task.seconds["finish"] == pytest.approx(300.0)
        assert task.seconds["work"] == pytest.approx(DAY - 300.0)
        assert task.seconds["review"] == 0.0
        assert task.seconds["waiting"] == 0.0

    def test_two_round_trips_sum_every_visit_to_review(self, corpus) -> None:
        """Section 17.4: review is every visit, revise time is work, finish is the
        hour from the last approval to the close."""
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-102"]

        assert task.review_rounds == 2
        assert task.approvals == 1
        assert task.seconds["queue"] == pytest.approx(DAY)
        assert task.seconds["review"] == pytest.approx(12 * HOUR + DAY)
        assert task.seconds["work"] == pytest.approx(DAY + 12 * HOUR)
        assert task.seconds["finish"] == pytest.approx(HOUR)
        assert task.total == pytest.approx(4 * DAY + HOUR)

    def test_a_reopen_puts_the_closed_interval_in_no_segment(self, corpus) -> None:
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-103"]

        assert task.total == pytest.approx(DAY + DAY), "half a day queued, half worked, then a day"
        assert task.seconds["queue"] == pytest.approx(12 * HOUR)
        assert task.seconds["work"] == pytest.approx(12 * HOUR + DAY)
        assert not task.excluded

    def test_two_claims_with_a_release_between_count_both_queue_spells(self, corpus) -> None:
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-104"]

        assert task.seconds["queue"] == pytest.approx(DAY + 12 * HOUR)
        assert task.seconds["work"] == pytest.approx(12 * HOUR + 12 * HOUR)
        assert task.estimated, "its creation row was reconstructed"

    def test_a_close_by_an_import_row_is_excluded_from_the_sample(self, corpus) -> None:
        projection, _store = corpus
        tasks = projection.segment_tasks(window_for(projection, "all"))

        assert tasks["task-105"].excluded
        points, _coverage = projection.segments(
            window_for(projection, "all"), projection.coverage(resolve_zone("America/Chicago"))
        )
        point = point_at(points, week_of(NOW - timedelta(days=4)))
        assert point["excluded"] == 1
        assert "task-105" not in [t for t in tasks if not tasks[t].excluded]

    def test_draft_time_is_waiting_and_queue_begins_at_promotion(self, corpus) -> None:
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-106"]

        assert task.seconds["waiting"] == pytest.approx(DAY + 12 * HOUR)
        assert task.seconds["queue"] == pytest.approx(12 * HOUR)
        assert task.seconds["work"] == pytest.approx(DAY)
        assert task.seconds["review"] == 0.0

    def test_approve_and_close_is_an_approval_with_no_finish(self, corpus) -> None:
        """Section 17.2: a close whose ball was on review is an approval."""
        projection, _store = corpus
        task = projection.segment_tasks(window_for(projection, "all"))["task-107"]

        assert task.approvals == 1
        assert task.review_rounds == 1
        assert task.seconds["review"] == pytest.approx(12 * HOUR)
        assert task.seconds["finish"] == 0.0

    def test_cancelled_and_open_tasks_are_in_no_sample(self, corpus) -> None:
        projection, _store = corpus
        tasks = projection.segment_tasks(window_for(projection, "all"))

        assert "task-108" not in tasks
        assert "task-109" not in tasks

    def test_the_bucket_carries_percentiles_sample_and_among(self, corpus) -> None:
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, coverage = projection.segments(window, projection.coverage(window.zone))

        assert coverage.bucket == "week"
        # The week of 7 Sep holds task-102, task-103, task-104 and task-106.
        point = point_at(points, week_of(NOW - timedelta(days=9)))
        assert point["sample"] == 4
        assert point["unreviewed"] == 3
        assert set(point["among"]) == set(SEGMENTS)
        for name in SEGMENTS:
            assert f"{name}_p50_hours" in point and f"{name}_p90_hours" in point
        # Hours, per segment, over the bucket's tasks: queue is 24, 12, 36 and 12.
        assert point["queue_p50_hours"] == pytest.approx(18.0)
        assert point["queue_p90_hours"] == pytest.approx(32.4)
        # Review is nonzero on one task of the four; the among-median is that task's.
        assert point["review_p50_hours"] == 0.0
        assert point["among"]["review"] == {"tasks": 1, "p50_hours": 36.0}
        assert point["among"]["waiting"] == {"tasks": 1, "p50_hours": 36.0}
        # Totals are 97, 48, 60 and 72 hours; the median interpolates the middle two.
        assert point["total_p50_hours"] == pytest.approx(66.0)

    def test_first_review_is_first_claim_to_first_review_entry(self, corpus) -> None:
        """S3, bucketed by the review entry: task-102 took a day, task-107 ten hours."""
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, _coverage = projection.segments(window, projection.coverage(window.zone))

        reviews = {
            p["bucket"]: (p["first_review_sample"], p["first_review_p50_hours"]) for p in points
        }
        assert reviews[week_of(NOW - timedelta(days=13))] == (1, 24.0)
        assert reviews[week_of(NOW - timedelta(days=2, hours=12))] == (1, 10.0)
        total = sum(sample for sample, _ in reviews.values())
        assert total == 2, "task-102 and task-107 are the only reviewed completed tasks"

    def test_a_bucket_with_a_reconstructed_task_is_estimated_and_the_others_are_not(
        self, corpus
    ) -> None:
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, _coverage = projection.segments(window, projection.coverage(window.zone))

        estimated = {p["bucket"] for p in points if p["estimated"]}
        assert week_of(NOW - timedelta(days=5, hours=12)) in estimated  # task-104
        assert week_of(NOW - timedelta(days=18)) not in estimated  # task-101


# ---------------------------------------------------------------------------
# Section 18.3 to 18.5: finishes, gates, cost per task, runs, the machine
# ---------------------------------------------------------------------------


class TestFinishes:
    def test_outcomes_reasons_and_durations_per_week(self, corpus) -> None:
        projection, _store = corpus
        points, coverage = projection.finishes(window_for(projection, "30d"))

        # fin_a (31 Aug) and fin_b (5 Sep) fall in the same week.
        week = point_at(points, week_of(NOW - timedelta(days=18)))
        assert week["finished"] == 1 and week["escalated"] == 1
        assert week["declined"] == 0 and week["interrupted"] == 0
        assert week["reasons"] == {"gate_failed": 1}
        assert week["estimated"], "fin_b is an imported row"
        assert week["duration_p50_min"] == pytest.approx(5.0), "escalated rows are not in it"
        assert week["sample"] == 1
        assert week["steps_p50_s"] == {"preflight": 1.0, "gate": 220.0, "merge": 2.0}
        assert week["runway_waited"] == 0 and week["runway_p90_s"] is None

    def test_a_skipped_step_is_not_in_its_steps_sample_and_a_runway_wait_is_counted(
        self, corpus
    ) -> None:
        projection, _store = corpus
        points, _coverage = projection.finishes(window_for(projection, "30d"))
        week_c = point_at(points, week_of(NOW - timedelta(days=11)))

        assert "verify" not in week_c["steps_p50_s"]
        assert week_c["runway_waited"] == 1
        assert week_c["runway_p90_s"] == pytest.approx(30.0)

    def test_the_series_starts_where_the_finishes_start_not_at_the_range(self, corpus) -> None:
        """Section 21.1: the task history reaches back thirty days; finishes eighteen."""
        projection, _store = corpus
        window = window_for(projection, "all")
        points, coverage = projection.finishes(window)

        assert coverage.recorded_from == NOW - timedelta(days=18, seconds=295)
        assert coverage.native_from == coverage.recorded_from
        assert points[0]["bucket"] == week_of(coverage.recorded_from)
        assert points[0]["bucket"] > window.first_day
        assert coverage.note is not None and "recorded from" in coverage.note
        assert not coverage.complete

    def test_a_project_with_no_finishes_says_so_rather_than_drawing_zero(
        self, tmp_path: Path
    ) -> None:
        store = build_project(tmp_path / "bare", "bare")
        plant(
            store,
            "task-001",
            [ev(NOW - timedelta(days=3), "create", lt="ready", bt="agent", rt="available")],
            lifecycle="ready",
            ball="agent",
            ball_reason="available",
            position=100,
        )
        projection = AnalyticsProjection(store.read_connection(), "bare", now=NOW)
        points, coverage = projection.finishes(window_for(projection, "30d"))

        assert points == []
        assert coverage.recorded_from is None
        assert coverage.note == "No finishes recorded yet."


class TestGates:
    def test_full_gates_green_rate_failed_stages_and_origins(self, corpus) -> None:
        projection, _store = corpus
        points, coverage = projection.gates(window_for(projection, "30d"))

        # All three full gates start in the week of 31 Aug; man_1 is partial.
        week = point_at(points, week_of(NOW - timedelta(days=13)))
        assert week["full"] == 3
        assert week["passed"] == 2
        assert week["failed_stages"] == {"pytest": 1}
        assert week["origins"] == {"finish": 1, "run": 2}
        assert week["duration_p50_min"] == pytest.approx(4.5), "green gates only"
        assert week["sample"] == 2
        assert week["stages_p50_s"] == {"pytest": 150.0, "e2e": 95.0}
        assert week["estimated"], "g1 is imported"

    def test_the_coverage_names_both_baselines_when_they_differ(self, corpus) -> None:
        """Finisher gates come by import from one date; agent-side ones from a later one."""
        projection, _store = corpus
        points, coverage = projection.gates(window_for(projection, "all"))

        assert coverage.recorded_from == NOW - timedelta(days=18, seconds=290)
        assert coverage.native_from == NOW - timedelta(days=13) + timedelta(hours=1)
        assert coverage.note is not None
        assert "agent-side and manual gates only from" in coverage.note
        assert point_at(points, week_of(coverage.recorded_from))["estimated"]


class TestCostPerTask:
    def test_runs_finishes_and_gate_minutes_per_completed_task(self, corpus) -> None:
        projection, _store = corpus
        window = window_for(projection, "30d")
        _f, finish_coverage = projection.finishes(window)
        _g, gate_coverage = projection.gates(window)
        points, coverage = projection.cost_per_task(window, finish_coverage, gate_coverage)

        # The week of 7 Sep closes task-102 (two runs, two finishes, 360 s of full
        # gates) and task-103, task-104 and task-106, which nobody dispatched.
        week = point_at(points, week_of(NOW - timedelta(days=11) + timedelta(hours=1)))
        assert week["sample"] == 4
        assert week["runs_mean"] == pytest.approx(0.5)
        assert week["runs_mode"] == 0
        assert week["finishes_mean"] == pytest.approx(0.5)
        assert week["gate_minutes_p50"] == pytest.approx(6.0)
        assert week["without_gate"] == 3
        assert week["estimated"], "task-102's escalated finish is an imported row"

        week_101 = point_at(points, week_of(NOW - timedelta(days=18)))
        assert week_101["runs_mean"] == pytest.approx(1.0)
        assert week_101["gate_minutes_p50"] == pytest.approx(4.0)
        assert week_101["estimated"], "task-101's gate row is imported"

        # A task nobody dispatched counts as zero runs and no gate.
        week_107 = point_at(points, week_of(NOW - timedelta(days=2)))
        assert week_107["without_gate"] >= 1
        assert coverage.recorded_from is not None


class TestRuns:
    def test_runs_per_spine_bucket_with_triggers_outcomes_and_hours(self, corpus) -> None:
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, coverage = projection.runs(window)

        assert coverage.bucket == window.bucket == "day"
        day = local_day(NOW - timedelta(days=14), window.zone)
        point = point_at(points, day)
        assert point["runs"] == 1
        assert point["triggers"] == {"child": 1}
        assert point["agent_hours"] == pytest.approx(2.0)
        assert point["outcomes"] == {"completed": 1}
        assert point["duration_p50_min"] == pytest.approx(120.0)

        in_flight = point_at(points, local_day(NOW - timedelta(days=2), window.zone))
        assert in_flight["runs"] == 1
        assert in_flight["in_flight"] == 1
        assert in_flight["outcomes"] == {}
        assert in_flight["sample"] == 0

        assert coverage.recorded_from == NOW - timedelta(days=19)
        assert all(not point["estimated"] for point in points)


class TestMachine:
    def test_latency_queue_wait_and_paused_hours_from_the_journal(self, corpus) -> None:
        projection, _store = corpus
        points, coverage = projection.machine(window_for(projection, "30d"))

        assert coverage.recorded_from == NOW - timedelta(days=19)
        week = point_at(points, week_of(NOW - timedelta(days=14)))
        assert week["admitted"] == 1, "the other project's admission is not counted"
        assert week["start_latency_p50_s"] == pytest.approx(4.0)
        assert week["queued"] == 1
        assert week["queue_wait_p50_s"] == pytest.approx(90.0)

        paused = point_at(points, week_of(NOW - timedelta(days=9)))
        assert paused["paused_waiters"] == 2
        assert paused["paused_run_hours"] == pytest.approx(1.5), "run-hours lost, not wall-clock"

    def test_without_a_journal_the_series_is_absent_and_says_so(self, tmp_path: Path) -> None:
        store = build_project(tmp_path / "solo", "solo")
        projection = AnalyticsProjection(store.read_connection(), "solo", now=NOW, execution=None)
        points, coverage = projection.machine(window_for(projection, "30d"))

        assert points == []
        assert coverage.note == "The execution journal was not read."

    def test_a_journal_that_refuses_costs_one_series_not_the_page(self, corpus) -> None:
        projection, _store = corpus

        class Refusing:
            def read(self, sql: str, params: Any = ()) -> Any:
                raise sqlite3.OperationalError("database is locked")

        projection.execution = Refusing()
        points, coverage = projection.machine(window_for(projection, "30d"))

        assert points == []
        assert coverage.note is not None and "could not be read" in coverage.note


# ---------------------------------------------------------------------------
# Section 18.6 and 18.7: review and questions
# ---------------------------------------------------------------------------


class TestReview:
    def test_every_exit_from_review_is_a_wait_and_only_some_are_approvals(self, corpus) -> None:
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, coverage = projection.review(window, projection.coverage(window.zone))

        by_week = {p["bucket"]: p for p in points}
        revise = by_week[week_of(NOW - timedelta(hours=12 * 25))]
        assert revise["exits"] >= 1
        approval = by_week[week_of(NOW - timedelta(days=11))]
        assert approval["approvals"] >= 1
        assert approval["first_time_approvals"] == 0, "task-102 was on its second round"
        closed = by_week[week_of(NOW - timedelta(days=2))]
        assert closed["approvals"] == 1 and closed["first_time_approvals"] == 1
        assert closed["wait_p50_hours"] == pytest.approx(12.0)

        assert sum(p["exits"] for p in points) == 3
        assert sum(p["approvals"] for p in points) == 2
        assert coverage.bucket == "week"

    def test_questions_are_bucketed_by_when_asked_and_answers_counted_once(self, corpus) -> None:
        """The handoff written beside an answer also carries ``re`` and must not count."""
        projection, _store = corpus
        window = window_for(projection, "30d")
        points, _coverage = projection.review(window, projection.coverage(window.zone))

        week = point_at(points, week_of(NOW - timedelta(days=13) + timedelta(hours=6)))
        assert week["questions"] >= 1
        assert week["answered"] == 1
        assert week["answer_p50_hours"] == pytest.approx(2.0)
        assert sum(p["questions"] for p in points) == 3
        assert sum(p["answered"] for p in points) == 1

    def test_in_review_now_lists_the_open_task_with_its_wait(self, corpus) -> None:
        projection, _store = corpus
        waiting = projection.in_review()

        assert [item["task_id"] for item in waiting] == ["task-108"]
        assert waiting[0]["title"] == "Title of task-108"
        assert waiting[0]["hours_waiting"] == pytest.approx(6.0)

    def test_open_questions_are_the_unanswered_ones_on_open_tasks_only(self, corpus) -> None:
        projection, _store = corpus
        questions = projection.open_questions()

        assert [(q["task_id"], q["entry_id"]) for q in questions] == [("task-108", 1)]
        assert questions[0]["hours_open"] == pytest.approx(48.0)


# ---------------------------------------------------------------------------
# Section 21: the endpoint, the models, the plans
# ---------------------------------------------------------------------------


class TestTheEndpoint:
    def test_every_new_series_is_on_the_response_with_its_coverage(self, served) -> None:
        client, _store, _reference = served
        response = client.get(f"/api/projects/{PROJECT}/analytics", params={"range": "30d"})

        assert response.status_code == 200, response.text
        payload = response.json()
        for series in (
            "segments",
            "cost_per_task",
            "finishes",
            "gates",
            "runs",
            "machine",
            "review",
        ):
            assert isinstance(payload[series], list), series
            coverage_key = "cost_coverage" if series == "cost_per_task" else f"{series}_coverage"
            assert set(payload[coverage_key]) == {
                "recorded_from",
                "native_from",
                "complete",
                "bucket",
                "note",
            }
        assert payload["in_review"][0]["task_id"] == "task-108"
        assert payload["open_questions"][0]["task_id"] == "task-108"

    @pytest.mark.parametrize("key", ["30d", "90d", "12m", "all"])
    def test_every_range_is_two_hundred_and_parses_no_task_document(self, served, key: str) -> None:
        client, _store, _reference = served
        response = client.get(f"/api/projects/{PROJECT}/analytics", params={"range": key})

        assert response.status_code == 200, response.text
        assert response.headers["X-Task-Parses"] == "0"
        assert response.json()["segments"], key

    def test_the_segments_over_http_are_relative_to_the_clock_the_endpoint_reads(
        self, served
    ) -> None:
        client, _store, reference = served
        payload = client.get(f"/api/projects/{PROJECT}/analytics", params={"range": "30d"}).json()

        zone = resolve_zone(payload["range"]["timezone"])
        week = bucket_start(local_day(reference - timedelta(days=18), zone), "week")
        point = next(p for p in payload["segments"] if p["bucket"] == week.isoformat())
        assert point["sample"] >= 1
        assert point["unreviewed"] >= 1
        assert point["among"]["finish"]["tasks"] >= 1

    def test_an_empty_project_answers_with_absent_series_not_zeros(self, served) -> None:
        client, _store, _reference = served
        payload = client.get("/api/projects/empty/analytics").json()

        assert payload["segments"] == [] and payload["finishes"] == [] and payload["runs"] == []
        assert payload["finishes_coverage"]["recorded_from"] is None
        assert payload["finishes_coverage"]["note"] == "No finishes recorded yet."
        assert payload["in_review"] == [] and payload["open_questions"] == []

    def test_the_totals_still_reconcile_with_the_dashboard(self, served) -> None:
        """Section 5.3's test is unchanged in spirit: ``totals`` did not move."""
        from agentjobs.dashboard import build_dashboard_snapshot
        from agentjobs.manager import TaskManager

        client, store, _reference = served
        totals = client.get(f"/api/projects/{PROJECT}/analytics").json()["totals"]
        stats = build_dashboard_snapshot(TaskManager(store))["stats"]

        for key in (
            "total",
            "in_progress",
            "blocked",
            "waiting_for_human",
            "awaiting_input",
            "completed",
        ):
            assert totals[key] == stats[key], key

    def test_every_new_model_is_named_in_the_document(self, served) -> None:
        client, _store, _reference = served
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

        assert {
            "SeriesCoverage",
            "SegmentPoint",
            "SegmentAmong",
            "CostPerTaskPoint",
            "FinishPoint",
            "GatePoint",
            "RunPoint",
            "MachinePoint",
            "ReviewPoint",
            "InReview",
            "OpenQuestion",
        } <= set(schemas)


class TestNoRunnerDimension:
    """Section 16, decision 12: no series is split by runner. Section 21.5 asks for this."""

    MODELS = (
        "SeriesCoverage",
        "SegmentPoint",
        "SegmentAmong",
        "CostPerTaskPoint",
        "FinishPoint",
        "GatePoint",
        "RunPoint",
        "MachinePoint",
        "ReviewPoint",
        "InReview",
        "OpenQuestion",
        "ThroughputPoint",
    )

    @pytest.mark.parametrize("name", MODELS)
    def test_no_field_is_a_runner_or_an_agent(self, name: str) -> None:
        """``agent_hours`` (R-2) is a total over every run and names no agent."""
        model = getattr(api_models, name)
        for field_name in model.model_fields:
            assert "runner" not in field_name, f"{name}.{field_name}"
            assert field_name != "agent", f"{name}.{field_name}"

    def test_no_new_query_reads_a_runner_or_agent_column(self) -> None:
        """``'agent'`` as a ball value is the holder rule; ``agent`` as a column is not.

        Scoped to the second set: the first set's holder series is banded by ball
        holder, and ``agent`` there is the band, which section 16 did not revisit.
        """
        first_new = list(QUERIES).index("segment events")
        new_queries = dict(list(QUERIES.items())[first_new:])
        for name, sql in {**new_queries, **EXECUTION_QUERIES}.items():
            text = sql.lower().replace("'agent'", "")
            assert not re.search(r"\brunner\b", text), name
            assert not re.search(r"\bagent\b", text), name


class TestQueryPlans:
    """ac-5: every new query is an indexed read, and none touches a JSON column.

    The plan is what is asserted, not the time, for the reason the first set gives. A
    structural line -- a temp b-tree for a sort or a group, a correlated subquery
    header -- is not a scan; a ``SCAN`` is, and only the two machine-level queries
    named in ``UNINDEXED_QUERIES`` are allowed one, with their reason.
    """

    STRUCTURE = (
        "USE TEMP B-TREE",
        "CORRELATED SCALAR SUBQUERY",
        "SCALAR SUBQUERY",
        "LIST SUBQUERY",
    )

    @pytest.fixture()
    def store(self, tmp_path: Path) -> SqlTaskStore:
        store = build_project(tmp_path / "plans", "plans")
        seed_process(store, NOW)
        return store

    @staticmethod
    def _assert_indexed(name: str, steps: Sequence[str]) -> None:
        assert steps, name
        for step in steps:
            if step.startswith(TestQueryPlans.STRUCTURE) or step.startswith("BLOOM FILTER"):
                continue
            if name in UNINDEXED_QUERIES and step.startswith("SCAN"):
                continue
            assert "USING INDEX" in step or "USING COVERING INDEX" in step, f"{name}: {step}"
            assert not step.startswith("SCAN"), f"{name}: {step}"

    @pytest.mark.parametrize("name", sorted(QUERIES))
    def test_the_query_is_answered_by_an_index(self, store: SqlTaskStore, name: str) -> None:
        sql = QUERIES[name]
        rows = (
            store.read_connection()
            .execute("EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?"))))
            .fetchall()
        )
        self._assert_indexed(name, [row["detail"] for row in rows])

    @pytest.mark.parametrize("name", sorted(EXECUTION_QUERIES))
    def test_the_journal_query_is_answered_by_an_index_or_exempted_by_name(
        self, tmp_path: Path, name: str
    ) -> None:
        journal = execution_store_for(tmp_path / "home")
        try:
            sql = EXECUTION_QUERIES[name]
            rows = journal.read(
                "EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?")))
            )
            self._assert_indexed(name, [row["detail"] for row in rows])
        finally:
            close_execution_stores()

    def test_every_exemption_names_a_query_and_a_reason(self) -> None:
        for name, reason in UNINDEXED_QUERIES.items():
            assert name in EXECUTION_QUERIES, name
            assert len(reason) > 40, name

    @pytest.mark.parametrize("name", sorted({**QUERIES, **EXECUTION_QUERIES}))
    def test_the_query_touches_no_json_column(self, name: str) -> None:
        sql = {**QUERIES, **EXECUTION_QUERIES}[name].lower()
        assert "json" not in sql
        assert "_json" not in sql

    def test_the_whole_payload_is_one_bounded_set_of_reads(self, store: SqlTaskStore) -> None:
        """Thirty-two statements on the task store is what sections 7.1 and 21 cost.

        A regression that put a query inside a loop -- one per task, one per bucket --
        would not change any plan above and would change this.

        F6 (task-586) adds five on this store: the scored landings, the reset marker
        and the three the uncorrected medians need. Each is a set query, so a store
        with landings to score pays three more for their predictions and outliers, and
        never one per landing.
        """
        connection = store.read_connection()
        executed: List[str] = []
        connection.set_trace_callback(executed.append)
        try:
            AnalyticsProjection(connection, "plans", now=NOW).build("all")
        finally:
            connection.set_trace_callback(None)

        assert len(executed) <= 37, executed

    def test_the_fold_is_in_python_not_in_a_window_function(self) -> None:
        """Section 17.6's decision, kept: window functions were measured slower."""
        for name, sql in QUERIES.items():
            assert " over (" not in sql.lower(), name
