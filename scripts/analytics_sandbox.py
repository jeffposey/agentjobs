"""Stand up the analytics page on its own port, with throwaway data.

Two projects on one server, because the page's hardest property is a state it is *not*
usually in. Switch between them with the project picker.

    sandbox-deep    nine months of history, five sources with five different
                    baselines, and every treatment on screen at once: reconstructed
                    buckets, weeks under the percentile minimum, a series that starts
                    six weeks into the window, a day the machine spent paused on a
                    usage limit, a reopened task, and a stuck panel whose largest
                    group is the queue
    sandbox-thin    four days old. Trends are suppressed and values are not; most of
                    the second set has no rows at all, and each panel has to say so
                    in a sentence rather than draw an axis at zero

The comparison is the fixture. A page that renders the deep project beautifully and
draws a flat line at zero for the thin one has failed at the thing sections 9.1 to 9.3
exist for, and only one of the two would show it.

    python scripts/analytics_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process. Nothing here touches the live corpus:
everything lives under a temporary directory that is deleted when this process stops,
including its own ``AGENTJOBS_HOME`` registry, so the 8876 dashboard and its registry
are not involved at all.

**The execution journal is seeded too.** R-5 and R-6 read ``execution.db`` rather than
the task store, and a sandbox that skipped it would leave the runs panel telling the
truth about one source and nothing about the other -- which is exactly the per-series
coverage the page is being reviewed for.
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

DEFAULT_PORT = 8897

#: The clock every seeded instant is measured back from. Wall clock, deliberately: the
#: page compares its own ``now`` against the coverage baseline, so a fixed anchor would
#: drift into "thin history" and then into "no history" as the week went on.
NOW = datetime.now(timezone.utc)

#: One seed for the whole sandbox, so two people looking at it see the same numbers.
RANDOM = random.Random(474)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ago(days: float = 0.0, hours: float = 0.0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


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


_PLACES = iter(range(100_000, 999_999, 100))


def _next_place() -> int:
    """The next free place in line, for a fixture that does not care which."""
    return next(_PLACES)


def plant(
    store,
    task_id: str,
    title: str,
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

    Straight SQL, the same as ``tests/test_analytics_second_set.py``'s planter: these
    fixtures are about the *shape* of the history the projection folds, and going
    through the manager would write today's instant on every event.
    """
    created = events[0]["ts"]
    # ``(lifecycle = 'closed') = (queue_position IS NULL)`` is a CHECK in the store's
    # own schema: an open task has a place in line and a closed one does not. The
    # sandbox satisfies it the way a real store does rather than being refused.
    place = None if lifecycle == "closed" else (position if position is not None else _next_place())
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO task(project_id, task_id, title, created_at, updated_at,"
            " lifecycle, ball, ball_reason, ball_prompt, outcome, archived, priority,"
            " queue_position, category, spec_summary, spec_description, closed_at,"
            " last_activity_at, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                task_id,
                title,
                iso(created),
                iso(closed_at or created),
                lifecycle,
                ball,
                ball_reason,
                None
                if ball_reason in (None, "available")
                else "Look at this and say whether it is right.",
                outcome,
                0,
                RANDOM.choice(["high", "medium", "medium", "low"]),
                place,
                "engineering",
                f"{title}. Seeded for an analytics review; nothing here is real work.",
                "Seeded for an analytics review. Nothing here is real work.",
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


def plant_run(
    store,
    run_id: str,
    task_id: str,
    started: datetime,
    *,
    seconds: float,
    outcome: Optional[str] = "completed",
    trigger: str = "manual",
) -> None:
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO task_run(project_id, run_id, task_id, agent, runner, mode, posture,"
            " trigger, caused_by, git_head, cwd, argv_json, started_at, ended_at, outcome,"
            " duration_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                store.project_id,
                run_id,
                task_id,
                "claude",
                "claude-cli",
                "session",
                "auto",
                trigger,
                1,
                "abc1234",
                "C:/sandbox",
                "[]",
                iso(started),
                iso(started + timedelta(seconds=seconds)) if outcome else None,
                outcome,
                seconds if outcome else None,
            ),
        )


def plant_log(
    store,
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
            (
                store.project_id,
                task_id,
                entry_id,
                iso(ts),
                "claude",
                entry_type,
                "Seeded for an analytics review.",
                re_entry,
            ),
        )


TITLES = [
    "Rebuild the queue projection",
    "The review panel, one screen",
    "Dispatch parks on a permission prompt",
    "Reorder by drag on a phone",
    "Backfill the reconstructed span",
    "Split the gate into named stages",
    "Restart after a merge, verified",
    "The attention badge, debounced",
    "Serve the bundle from the package",
    "One gate per handoff",
    "The epic walk, watched down",
    "Per-series coverage on the payload",
]


def seed_deep(store, *, days: int) -> None:
    """Nine months of history with every treatment on screen at once.

    Written as a loop over completed tasks rather than as twelve hand-planted cases,
    because what is being reviewed is a *page of trends* -- a fixture of six tasks
    draws six bars and says nothing about whether ninety are legible. The hand-planted
    part is the exceptions: the reopen, the unreviewed close, the two round-trips and
    the weeks deliberately left under the percentile minimum.
    """
    finish_from = ago(days=42)
    gate_from = ago(days=42)
    runs_from = ago(days=13)
    number = 0
    finish_number = 0
    gate_number = 0
    run_number = 0

    for day in range(days, 0, -1):
        # Roughly two completions a day, with quiet days and busy ones, so the
        # throughput bars have a shape rather than a plateau.
        for _ in range(RANDOM.choice([0, 1, 1, 2, 2, 3, 5])):
            number += 1
            task_id = f"task-{number:03d}"
            title = TITLES[number % len(TITLES)]
            # Everything older than 45 days is reconstructed -- the whole task, not
            # only its creation, because that is what a git backfill produces.
            #
            # 45 rather than 60 so **both** halves of section 19.1 are on this one
            # project: on the 90-day range native history begins inside the window and
            # the tiles read *"since <date>"*, and on the 30-day range it does not and
            # they read *"in 30 days"*. One tap between the two states is the whole
            # point of seeding them together.
            source = "reconstructed" if day > 45 else "native"
            created = ago(days=day + RANDOM.choice([0, 1, 3, 9, 40]))
            claimed = created + timedelta(hours=RANDOM.choice([1, 6, 30]))
            reviewed = claimed + timedelta(hours=RANDOM.choice([1, 2, 8]))
            approved = reviewed + timedelta(hours=RANDOM.choice([0.1, 0.5, 5, 30]))
            closed = approved + timedelta(minutes=RANDOM.choice([4, 6, 20]))
            events = [
                ev(created, "create", lt="ready", bt="agent", rt="available", source=source),
                ev(
                    claimed,
                    "claim",
                    lf="ready",
                    lt="active",
                    bf="agent",
                    rf="available",
                    bt="agent",
                    rt="work",
                    source=source,
                ),
            ]
            # One task in five goes round twice, which is what the first-time approval
            # rate is a rate of.
            if number % 5 == 0:
                events.append(
                    ev(
                        reviewed,
                        "handoff",
                        lf="active",
                        lt="active",
                        bf="agent",
                        rf="work",
                        bt="human",
                        rt="review",
                        source=source,
                    )
                )
                events.append(
                    ev(
                        reviewed + timedelta(hours=2),
                        "handoff",
                        lf="active",
                        lt="active",
                        bf="human",
                        rf="review",
                        bt="agent",
                        rt="revise",
                        source=source,
                    )
                )
                events.append(
                    ev(
                        reviewed + timedelta(hours=5),
                        "handoff",
                        lf="active",
                        lt="active",
                        bf="agent",
                        rf="revise",
                        bt="human",
                        rt="review",
                        source=source,
                    )
                )
            elif number % 7 != 0:
                events.append(
                    ev(
                        reviewed,
                        "handoff",
                        lf="active",
                        lt="active",
                        bf="agent",
                        rf="work",
                        bt="human",
                        rt="review",
                        source=source,
                    )
                )
            # One in seven is never reviewed at all: its finish segment comes from the
            # merged finish row, which is section 17.3's case.
            if number % 7 != 0:
                events.append(
                    ev(
                        approved,
                        "handoff",
                        lf="active",
                        lt="active",
                        bf="human",
                        rf="review",
                        bt="agent",
                        rt="work",
                        source=source,
                    )
                )
            outcome = "cancelled" if number % 23 == 0 else "completed"
            events.append(
                ev(
                    closed,
                    "close",
                    lf="active",
                    lt="closed",
                    bf="agent",
                    rf="work",
                    outcome=outcome,
                    source=source,
                )
            )
            plant(
                store,
                task_id,
                title,
                events,
                lifecycle="closed",
                outcome=outcome,
                closed_at=closed,
            )

            if closed >= finish_from and outcome == "completed":
                finish_number += 1
                escalated = finish_number % 6 == 0
                gate_seconds = RANDOM.choice([180.0, 240.0, 253.0, 300.0, 380.0])
                store.record_finish(
                    f"fin_{finish_number:04d}",
                    {
                        "task_id": task_id,
                        "started_at": iso(closed - timedelta(seconds=gate_seconds + 20)),
                        "finished_at": iso(closed),
                        "seconds": gate_seconds + 20,
                        "outcome": "escalated" if escalated else "finished",
                        "reason": "gate_failed" if escalated else None,
                        "stopped_at": "gate" if escalated else None,
                        "merged": not escalated,
                        "merge_commit": None if escalated else f"{finish_number:07x}",
                        "source": "imported" if closed < ago(days=21) else "native",
                    },
                    [
                        {
                            "seq": 1,
                            "step": "preflight",
                            "ok": True,
                            "seconds": 1.1,
                            "ts": iso(closed),
                        },
                        {
                            "seq": 2,
                            "step": "runway",
                            "ok": True,
                            "seconds": 42.0 if finish_number % 11 == 0 else 0.0,
                            "ts": iso(closed),
                        },
                        {
                            "seq": 3,
                            "step": "gate",
                            "ok": not escalated,
                            "seconds": gate_seconds,
                            "ts": iso(closed),
                        },
                        {
                            "seq": 4,
                            "step": "merge",
                            "ok": True,
                            "skipped": escalated,
                            "seconds": 1.2,
                            "ts": iso(closed),
                        },
                        {
                            "seq": 5,
                            "step": "restart",
                            "ok": True,
                            "skipped": escalated,
                            "seconds": 2.4,
                            "ts": iso(closed),
                        },
                    ],
                )
                if closed >= gate_from:
                    gate_number += 1
                    store.record_gate_run(
                        f"fin_{finish_number:04d}:g1",
                        {
                            "origin": "finish",
                            "finish_id": f"fin_{finish_number:04d}",
                            "task_id": task_id,
                            "scope": "full",
                            "started_at": iso(closed - timedelta(seconds=gate_seconds)),
                            "finished_at": iso(closed),
                            "seconds": gate_seconds,
                            "passed": not escalated,
                            "failed_stage": "pytest" if escalated else None,
                            "source": "imported" if closed < ago(days=21) else "native",
                        },
                        [
                            {
                                "seq": 1,
                                "stage": "pytest",
                                "seconds": gate_seconds * 0.5,
                                "passed": not escalated,
                                "started_at": iso(closed),
                            },
                            {
                                "seq": 2,
                                "stage": "e2e",
                                "seconds": gate_seconds * 0.42,
                                "passed": True,
                                "started_at": iso(closed),
                            },
                            {
                                "seq": 3,
                                "stage": "vitest",
                                "seconds": 12.0,
                                "passed": True,
                                "started_at": iso(closed),
                            },
                        ],
                    )

            if closed >= runs_from:
                for attempt in range(1 + (number % 3 == 0)):
                    run_number += 1
                    plant_run(
                        store,
                        f"run_{run_number:04d}",
                        task_id,
                        closed - timedelta(hours=RANDOM.choice([1, 3, 9])),
                        seconds=RANDOM.choice([900.0, 1620.0, 3600.0, 5820.0]),
                        outcome="interrupted" if attempt else "completed",
                        trigger=RANDOM.choice(["manual", "manual", "child", "auto"]),
                    )

    # A reopen, so the throughput marker and `completion_events` have something to say.
    reopened = ago(days=9)
    plant(
        store,
        "task-900",
        "Reopened after the restart did not take",
        [
            ev(ago(days=20), "create", lt="ready", bt="agent", rt="available"),
            ev(
                ago(days=19),
                "claim",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="agent",
                rt="work",
            ),
            ev(
                ago(days=12),
                "close",
                lf="active",
                lt="closed",
                bf="agent",
                rf="work",
                outcome="completed",
            ),
            ev(ago(days=11), "reopen", lf="closed", lt="active", bt="agent", rt="work"),
            ev(
                reopened,
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
        closed_at=reopened,
    )

    # The open work, which is what the stuck panel and the review list are about. The
    # queue is deliberately the largest group: section 19.3's whole point is that it
    # stops being the headline of a panel called "Stuck".
    open_number = 0

    def open_task(ball: str, reason: str, age: float) -> str:
        """One open task. Its place in line is required by the store's own CHECK.

        ``(lifecycle = 'closed') = (queue_position IS NULL)``: an open task has a place
        in the queue and a closed one does not, and the sandbox has to satisfy that as
        a real store does rather than passing None and being refused.
        """
        nonlocal open_number
        open_number += 1
        task_id = f"task-{800 + open_number:03d}"
        position = 1000 + open_number * 100
        created = ago(days=age + 2)
        events = [ev(created, "create", lt="ready", bt="agent", rt="available")]
        if not (ball == "agent" and reason == "available"):
            events.append(
                ev(
                    ago(days=age),
                    "claim" if reason == "work" else "handoff",
                    lf="ready",
                    lt="active",
                    bf="agent",
                    rf="available",
                    bt=ball,
                    rt=reason,
                )
            )
        plant(
            store,
            task_id,
            TITLES[open_number % len(TITLES)],
            events,
            lifecycle="ready" if reason == "available" else "active",
            ball=ball,
            ball_reason=reason,
            position=position,
        )
        return task_id

    for index in range(28):
        open_task("agent", "available", 3 + index * 1.5)
    for index in range(6):
        review_id = open_task("human", "review", 0.2 + index * 1.7)
        if index == 0:
            plant_log(store, review_id, 1, ago(days=2, hours=4), "question")
    for index in range(3):
        open_task("human", "decision", 1 + index * 4)
    for index in range(2):
        open_task("external", "dependency", 4 + index * 5)
    open_task("external", "service", 11)
    for index in range(4):
        open_task("agent", "work", 0.1 + index * 0.3)

    # Questions with answers, so Q-2 has a distribution rather than one point.
    for index in range(1, 14):
        asked = ago(days=index * 5)
        plant_log(store, f"task-{index:03d}", 1, asked, "question")
        if index % 4:
            plant_log(
                store,
                f"task-{index:03d}",
                2,
                asked + timedelta(hours=RANDOM.choice([0.2, 0.5, 4, 30])),
                "answer",
                re_entry=1,
            )


def seed_thin(store) -> None:
    """Four days old: values are facts, trends are not, and most sources have no rows.

    The second half is the point. A page that has been built against nine months of
    everything can draw a flat line at zero for a project with no finishes and no runs
    and look entirely healthy doing it -- which is the substitution section 9.3 forbids
    by name, and the only way to see it is to have one on screen.
    """
    for index in range(1, 6):
        closed = ago(days=index * 0.5)
        plant(
            store,
            f"task-{index:03d}",
            TITLES[index],
            [
                ev(closed - timedelta(days=2), "create", lt="ready", bt="agent", rt="available"),
                ev(
                    closed - timedelta(days=1),
                    "claim",
                    lf="ready",
                    lt="active",
                    bf="agent",
                    rf="available",
                    bt="agent",
                    rt="work",
                ),
                ev(
                    closed,
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
            closed_at=closed,
        )
    for index in range(6, 12):
        plant(
            store,
            f"task-{index:03d}",
            TITLES[index],
            [ev(ago(days=3), "create", lt="ready", bt="agent", rt="available")],
            lifecycle="ready",
            ball="agent",
            ball_reason="available",
            position=1000 + index * 100,
        )
    plant(
        store,
        "task-020",
        "Waiting on a person since this morning",
        [
            ev(ago(days=2), "create", lt="ready", bt="agent", rt="available"),
            ev(
                ago(hours=6),
                "handoff",
                lf="ready",
                lt="active",
                bf="agent",
                rf="available",
                bt="human",
                rt="review",
            ),
        ],
        lifecycle="active",
        ball="human",
        ball_reason="review",
    )


def seed_journal(execution_store, project_id: str, *, days: int) -> None:
    """R-5 and R-6, which live in ``execution.db`` rather than in the task store.

    Two different baselines again: admissions from a fortnight back, and the usage-limit
    waiters from five days back, because that instrumentation is younger. The runs panel
    then has to say two different things about two sources, which is the state per-series
    coverage exists for.
    """
    with execution_store.transaction("seed") as connection:
        for index in range(60):
            admitted = ago(days=13 - index * 0.2)
            if admitted < ago(days=13):
                continue
            connection.execute(
                "INSERT INTO run_attempt(run_id, project_id, task_id, takes_slot, holder,"
                " state, admitted_at, launched_at, concluded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"att_{index:04d}",
                    project_id,
                    f"task-{index % 40 + 1:03d}",
                    1,
                    "sandbox",
                    "terminal",
                    iso(admitted),
                    iso(admitted + timedelta(seconds=2.2)),
                    iso(admitted + timedelta(minutes=20)),
                ),
            )
            if index % 5 == 0:
                connection.execute(
                    "INSERT INTO dispatch_queue(queue_id, seq, project_id, task_id, status,"
                    " run_id, queued_at, claimed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"q_{index:04d}",
                        index,
                        project_id,
                        f"task-{index % 40 + 1:03d}",
                        "started",
                        f"att_{index:04d}",
                        iso(admitted - timedelta(seconds=90 + index)),
                        iso(admitted),
                        iso(admitted),
                    ),
                )
        stalled = ago(days=4)
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, state,"
            " opened_at, next_probe_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "inc_1",
                "usage_limit",
                "p",
                "{}",
                "recovered",
                iso(stalled),
                iso(stalled),
                iso(stalled),
            ),
        )
        for index, minutes in enumerate([50, 50, 50, 22, 40]):
            connection.execute(
                "INSERT INTO auth_waiter(incident_id, run_id, project_id, task_id, session_id,"
                " stall_at, status, joined_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "inc_1",
                    f"att_{index:04d}",
                    project_id,
                    f"task-{index + 1:03d}",
                    f"s{index}",
                    iso(stalled),
                    "recovered",
                    iso(stalled),
                    iso(stalled + timedelta(minutes=minutes)),
                ),
            )
    _ = days


def build(root: Path, *, project_id: str, name: str, deep: bool) -> Path:
    from agentjobs.manager import TaskManager  # noqa: F401 -- imported for parity
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    store = sandbox_store(project_root / "tasks", project_id=project_id)
    with store.database.write() as connection:
        connection.execute(
            "UPDATE project SET reporting_tz = ? WHERE project_id = ?",
            ("America/Chicago", project_id),
        )
    if deep:
        seed_deep(store, days=270)
    else:
        seed_thin(store)
    return project_root


def main() -> None:
    argv = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-analytics-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.execution.factory import execution_store_for
    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    projects: List[tuple[str, str, bool]] = [
        ("sandbox-deep", "Sandbox: nine months of everything", True),
        ("sandbox-thin", "Sandbox: four days old", False),
    ]
    for project_id, name, deep in projects:
        registry.add(
            build(root, project_id=project_id, name=name, deep=deep),
            project_id=project_id,
            name=name,
        )

    # The journal is machine-level and filtered by project, so it is seeded once for
    # the deep project only -- which is also what makes the thin one's runs panel say
    # "nothing recorded" rather than draw a zero.
    seed_journal(execution_store_for(home), "sandbox-deep", days=270)

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    print(f"[review] analytics sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for project_id, name, _ in projects:
        print(
            f"[review]   {name}: http://127.0.0.1:{port}/app/p/{project_id}/analytics", flush=True
        )
    print(
        "[review] both ranges are worth a look: it opens on 30 days, and 90 is one tap away.",
        flush=True,
    )
    print(f"[review] throwaway data under {root}", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
