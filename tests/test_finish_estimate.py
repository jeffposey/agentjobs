"""The landing estimate: progress and time remaining for a scripted finish (task-586).

Four layers, each asserting the numbers a reader is shown rather than the presence of a
field:

- **The model** (``TestTheEstimate``): pure arithmetic over a fixed history -- no
  history, the runway, mid-gate, the cap, an overrun, and the correction applied.
- **Self-measurement** (``TestItRecordsWhatItPredicted``): a finish replayed through the
  store the way the finisher and the gate write it leaves a prediction at each checkpoint,
  scored once the finish ends.
- **Self-correction** (``TestTheCorrection``): the bias factor converges on history that
  is consistently slower than the medians predict, is clamped, ignores outliers, and
  resets.
- **The surfaces** (``TestTheSurfacesAgree``): the task list and the task page carry the
  same estimate for one finish, from one computation on the server.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from agentjobs import finish_estimate as fe
from agentjobs.analytics import build_analytics
from agentjobs.api import landing_estimate
from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.finish_status import STEP_ORDER
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.sqlstore.connection import Database
from agentjobs.sqlstore.migrations import upgrade

from test_execution_controller import Machine, machine
from test_task_finishing_status import a_live_finish, hold_lock, record, write_finish

__all__ = ["machine"]

STEP_SECONDS: Dict[str, float] = {
    "preflight": 1.0,
    "runway": 0.0,
    "rebase": 1.0,
    "gate": 300.0,
    "catch_up": 0.0,
    "merge": 1.0,
    "rebuild": 6.0,
    "restart": 8.0,
    "verify": 1.0,
    "close": 2.0,
    "teardown": 1.0,
    "worktree": 3.0,
    "branch": 1.0,
}
"""A typical landing: 325 s with no runway wait, 300 of them the gate."""

TOTAL = sum(STEP_SECONDS.values())

STAGES: List[Tuple[str, float]] = [("mypy", 20.0), ("pytest", 200.0), ("e2e", 80.0)]

MODEL = fe.EstimateModel(
    steps=STEP_SECONDS,
    stages=tuple(STAGES),
    sample=20,
    gate_sample=20,
    typical_s=TOTAL,
)


def through(step: str) -> Tuple[Tuple[str, float], ...]:
    """Every step up to and including ``step`` as done, each taking its median."""
    index = STEP_ORDER.index(step)
    return tuple((name, STEP_SECONDS[name]) for name in STEP_ORDER[: index + 1])


# ----- the model ------------------------------------------------------------------------


class TestTheEstimate:
    def test_too_little_history_is_elapsed_time_only(self) -> None:
        thin = fe.EstimateModel(steps=STEP_SECONDS, sample=2)

        answer = fe.estimate(thin, fe.Position(current="gate"))

        assert answer.kind == fe.NO_HISTORY
        assert (answer.progress, answer.eta_seconds) == (None, None)
        assert "Fewer than 3 finished landings" in answer.basis

    def test_the_runway_is_indeterminate_and_invents_no_time(self) -> None:
        answer = fe.estimate(
            MODEL, fe.Position(done=through("preflight"), current="runway", current_elapsed_s=90)
        )

        assert answer.kind == fe.RUNWAY
        assert (answer.progress, answer.eta_seconds) == (None, None)
        assert "merge runway" in answer.basis

    def test_mid_gate_is_weighted_by_the_stages_and_not_by_the_step_count(self) -> None:
        """Three steps of thirteen are done, but the bar reads what the time says.

        Done: preflight and rebase (2 s). In the gate: mypy done (20 of 300) and 100 s
        into pytest's 200. So 2 + 120 = 122 of 325 expected seconds, and 100 s of pytest,
        80 of e2e and 23 of the steps after the gate still to go.
        """
        position = fe.Position(
            done=through("rebase"),
            current="gate",
            current_elapsed_s=120.0,
            stages_done=("mypy",),
            stage="pytest",
            stage_elapsed_s=100.0,
        )

        answer = fe.estimate(MODEL, position)

        assert answer.kind == fe.ESTIMATE
        assert answer.progress == round(122 / 325, 4)
        assert answer.eta_seconds == 100 + 80 + 23
        assert answer.overrun is False
        assert answer.typical_seconds == 325.0

    def test_the_bar_moves_during_the_gate(self) -> None:
        def at(stage: str, done: Tuple[str, ...], seconds: float) -> float:
            answer = fe.estimate(
                MODEL,
                fe.Position(
                    done=through("rebase"),
                    current="gate",
                    stages_done=done,
                    stage=stage,
                    stage_elapsed_s=seconds,
                ),
            )
            assert answer.progress is not None
            return answer.progress

        readings = [
            at("mypy", (), 0),
            at("mypy", (), 10),
            at("pytest", ("mypy",), 50),
            at("pytest", ("mypy",), 150),
            at("e2e", ("mypy", "pytest"), 40),
        ]

        assert readings == sorted(readings)
        assert len(set(readings)) == len(readings)
        assert readings[0] == round(2 / 325, 4)
        assert readings[-1] == round((2 + 20 + 200 + 40) / 325, 4)

    def test_a_running_finish_never_reads_done(self) -> None:
        answer = fe.estimate(
            MODEL, fe.Position(done=through("worktree"), current="branch", current_elapsed_s=30)
        )

        assert answer.progress == fe.PROGRESS_CAP
        assert answer.eta_seconds is not None and answer.eta_seconds >= 0

    def test_a_landing_past_its_whole_expected_time_is_an_overrun_not_a_negative(self) -> None:
        position = fe.Position(
            done=through("rebase"),
            current="gate",
            current_elapsed_s=400.0,
            stages_done=("mypy", "pytest"),
            stage="e2e",
            stage_elapsed_s=180.0,
        )

        answer = fe.estimate(MODEL, position)

        assert answer.overrun is True
        assert answer.eta_seconds == 23.0  # the steps after the gate; never below zero
        assert answer.progress is not None and answer.progress < fe.PROGRESS_CAP

    def test_a_runway_wait_already_behind_it_is_not_an_overrun(self) -> None:
        """Ten minutes on the runway is not the landing running slow."""
        done = tuple(
            (name, 600.0 if name == "runway" else seconds) for name, seconds in through("rebase")
        )

        answer = fe.estimate(MODEL, fe.Position(done=done, current="gate", current_elapsed_s=10))

        assert answer.overrun is False

    def test_the_learned_correction_scales_the_time_left_and_not_the_bar(self) -> None:
        corrected = fe.EstimateModel(
            steps=STEP_SECONDS,
            stages=tuple(STAGES),
            sample=20,
            gate_sample=20,
            bias=fe.Bias(factor=1.5, sample=6, learned=1.5),
        )
        position = fe.Position(done=through("rebase"), current="gate")

        plain = fe.estimate(MODEL, position)
        answer = fe.estimate(corrected, position)

        assert answer.progress == plain.progress
        assert answer.raw_eta_seconds == plain.eta_seconds == 323.0
        assert answer.eta_seconds == 484.5
        assert "corrected ×1.50" in answer.basis


# ----- a store with history -------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqlTaskStore]:
    database = Database(tmp_path / "agentjobs.db")
    upgrade(database, agentjobs_version="test", snapshot_before=False)
    task_store = SqlTaskStore(database, "demo")
    task_store.ensure_project(root="C:/demo")
    landing_estimate.forget()
    yield task_store
    database.close()


def make_task(store: SqlTaskStore) -> str:
    return (
        TaskManager(store)
        .create_task(
            title="A task", summary="A task.", description="Do it.", lifecycle=Lifecycle.READY
        )
        .id
    )


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def seed_landing(
    store: SqlTaskStore,
    finish_id: str,
    start: datetime,
    *,
    task_id: str,
    scale: float = 1.0,
    runway_wait: float = 0.0,
    retried: bool = False,
) -> datetime:
    """One finished landing, as the finisher and the gate would have recorded it."""
    moment = start
    steps: List[Dict[str, Any]] = []
    gate_start = start
    for seq, name in enumerate(STEP_ORDER, start=1):
        seconds = STEP_SECONDS[name] * scale + (runway_wait if name == "runway" else 0.0)
        if name == "gate":
            gate_start = moment
        moment += timedelta(seconds=seconds)
        steps.append({"seq": seq, "step": name, "ok": True, "seconds": seconds, "ts": iso(moment)})
    store.record_finish(
        finish_id,
        {
            "task_id": task_id,
            "started_at": iso(start),
            "finished_at": iso(moment),
            "seconds": (moment - start).total_seconds(),
            "outcome": "finished",
        },
        steps,
    )
    seed_gate(store, f"{finish_id}:g1", gate_start, finish_id=finish_id, scale=scale)
    if retried:
        seed_gate(store, f"{finish_id}:g2", gate_start, finish_id=finish_id, scale=scale)
    return moment


def seed_gate(
    store: SqlTaskStore, gate_id: str, start: datetime, *, finish_id: str, scale: float = 1.0
) -> None:
    moment = start
    stages: List[Dict[str, Any]] = []
    for seq, (name, seconds) in enumerate(STAGES, start=1):
        began = moment
        moment += timedelta(seconds=seconds * scale)
        stages.append(
            {
                "seq": seq,
                "stage": name,
                "seconds": seconds * scale,
                "passed": True,
                "started_at": iso(began),
                "finished_at": iso(moment),
            }
        )
    store.record_gate_run(
        gate_id,
        {
            "origin": "finish",
            "finish_id": finish_id,
            "scope": "full",
            "started_at": iso(start),
            "finished_at": iso(moment),
            "seconds": (moment - start).total_seconds(),
            "passed": True,
        },
        stages,
    )


def history(store: SqlTaskStore, count: int, start: datetime) -> datetime:
    task_id = make_task(store)
    moment = start
    for index in range(count):
        moment = seed_landing(store, f"fin_base{index:03d}", moment, task_id=task_id)
        moment += timedelta(minutes=5)
    return moment


def prediction(
    store: SqlTaskStore, finish_id: str, predicted_at: datetime, raw: float, bias: float = 1.0
) -> None:
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO finish_prediction(project_id, finish_id, checkpoint, predicted_at, "
            "elapsed_s, raw_eta_s, bias, eta_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                store.project_id,
                finish_id,
                fe.ACCURACY_CHECKPOINT,
                iso(predicted_at),
                2.0,
                raw,
                bias,
                raw * bias,
            ),
        )


START = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class TestTheModelFromHistory:
    def test_steps_are_measured_landing_to_landing_and_stages_in_running_order(
        self, store: SqlTaskStore
    ) -> None:
        now = history(store, 5, START)

        model = store.finish_estimate_model(now)

        assert model.sample == 5 and model.gate_sample == 5
        assert model.expected("gate") == 300.0
        assert model.expected("restart") == 8.0
        assert model.stages == tuple(STAGES)
        assert model.total_s == TOTAL
        assert model.typical_s == TOTAL

    def test_a_finish_that_had_not_ended_yet_does_not_count(self, store: SqlTaskStore) -> None:
        history(store, 5, START)

        model = store.finish_estimate_model(START + timedelta(minutes=12))

        assert model.sample == 1


class TestItRecordsWhatItPredicted:
    def test_a_replayed_landing_leaves_a_prediction_at_each_checkpoint(
        self, store: SqlTaskStore
    ) -> None:
        """Written the way the finisher and the gate write, and read back as scored."""
        start = history(store, 3, START)
        task_id = make_task(store)
        finish_id = "fin_live0001"
        steps: List[Dict[str, Any]] = []

        def write(outcome: str = "running", **extra: Any) -> None:
            store.record_finish(
                finish_id,
                {"task_id": task_id, "started_at": iso(start), "outcome": outcome, **extra},
                steps,
            )

        write()
        first = store.record_finish_checkpoint(finish_id, start)
        moment = start
        for seq, name in enumerate(("preflight", "runway", "rebase"), start=1):
            moment += timedelta(seconds=STEP_SECONDS[name])
            steps.append({"seq": seq, "step": name, "ok": True, "ts": iso(moment)})
        write()
        at_gate = store.record_finish_checkpoint(finish_id, moment)
        again = store.record_finish_checkpoint(finish_id, moment + timedelta(seconds=30))
        # The gate reports mypy finished: pytest starts now.
        store.record_gate_run(
            f"{finish_id}:g1",
            {
                "origin": "finish",
                "finish_id": finish_id,
                "scope": "full",
                "started_at": iso(moment),
            },
            [{"seq": 1, "stage": "mypy", "seconds": 20.0, "started_at": iso(moment)}],
        )
        at_pytest = store.record_finish_checkpoint(finish_id, moment + timedelta(seconds=20))

        assert first is not None and first.checkpoint == "step:preflight"
        assert first.raw_eta_s == TOTAL
        assert at_gate is not None and at_gate.checkpoint == "step:gate"
        assert at_gate.raw_eta_s == TOTAL - 2
        assert again is None  # the first prediction at a checkpoint is the one kept
        assert at_pytest is not None and at_pytest.checkpoint == "stage:pytest"
        assert at_pytest.raw_eta_s == 200 + 80 + 23

        # It lands 50% slower than predicted from the gate on.
        ended = moment + timedelta(seconds=at_gate.raw_eta_s * 1.5)
        write("finished", finished_at=iso(ended), seconds=(ended - start).total_seconds())
        scored = fe.measured(
            store.read_connection(), store.project_id, as_of=iso(ended + timedelta(seconds=1))
        )

        live = next(landing for landing in scored if landing.finish_id == finish_id)
        assert live.at_gate is not None
        eta, raw, actual, bias = live.at_gate
        assert (raw, bias) == (TOTAL - 2, 1.0)
        assert actual == pytest.approx(raw * 1.5)
        assert live.outlier is False

    def test_nothing_is_predicted_on_the_runway_or_without_history(
        self, store: SqlTaskStore
    ) -> None:
        task_id = make_task(store)
        store.record_finish(
            "fin_thin", {"task_id": task_id, "started_at": iso(START), "outcome": "running"}
        )
        assert store.record_finish_checkpoint("fin_thin", START) is None

        start = history(store, 3, START + timedelta(hours=1))
        store.record_finish(
            "fin_wait",
            {"task_id": task_id, "started_at": iso(start), "outcome": "running"},
            [{"seq": 1, "step": "preflight", "ok": True, "ts": iso(start)}],
        )
        assert store.record_finish_checkpoint("fin_wait", start) is None


class TestTheCorrection:
    """History seeded consistently slower than the medians predict."""

    def slow_landings(
        self, store: SqlTaskStore, ratios: List[float], start: datetime, **options: Any
    ) -> List[float]:
        """One finished landing per ratio, each predicted at ``1/ratio`` of what it took.

        Returns the bias factor in force after each one ends, which is the sequence a
        reader of the accuracy chart would have watched.
        """
        task_id = make_task(store)
        factors: List[float] = []
        moment = start
        for index, ratio in enumerate(ratios):
            finish_id = f"fin_{options.get('prefix', 'slow')}{index:03d}"
            ended = seed_landing(
                store, finish_id, moment, task_id=task_id, **options.get("seed", {})
            )
            predicted_at = moment + timedelta(seconds=2)
            prediction(
                store, finish_id, predicted_at, (ended - predicted_at).total_seconds() / ratio
            )
            factors.append(fe.learn_bias(store.read_connection(), store.project_id, ended).factor)
            moment = ended + timedelta(minutes=5)
        return factors

    def test_it_converges_on_a_consistent_miss(self, store: SqlTaskStore) -> None:
        start = history(store, 3, START)
        ratios = [1.4, 1.6, 1.5, 1.45, 1.55, 1.5, 1.48, 1.52]

        factors = self.slow_landings(store, ratios, start)

        assert factors[:2] == [1.0, 1.0]  # fewer than three measured: no correction yet
        assert all(1.4 <= factor <= 1.6 for factor in factors[2:])
        assert factors[-1] == pytest.approx(1.5, abs=0.02)
        model = store.finish_estimate_model(start + timedelta(days=1))
        assert model.bias.active and model.bias.factor == factors[-1]
        answer = fe.estimate(model, fe.Position(done=through("rebase"), current="gate"))
        assert answer.eta_seconds == pytest.approx(round((TOTAL - 2) * factors[-1], 1))

    def test_outliers_are_reported_but_do_not_move_it(self, store: SqlTaskStore) -> None:
        start = history(store, 3, START)
        self.slow_landings(store, [1.5, 1.5, 1.5, 1.5], start)
        before = fe.learn_bias(store.read_connection(), store.project_id, START + timedelta(days=1))

        later = START + timedelta(hours=6)
        self.slow_landings(store, [3.0, 3.0], later, prefix="retry", seed={"retried": True})
        self.slow_landings(
            store, [3.0, 3.0], later + timedelta(hours=2), prefix="wait", seed={"runway_wait": 400}
        )
        after = fe.learn_bias(store.read_connection(), store.project_id, START + timedelta(days=1))

        assert (before.factor, before.sample) == (1.5, 4)
        assert (after.factor, after.sample, after.excluded) == (1.5, 4, 4)
        scored = fe.measured(
            store.read_connection(), store.project_id, as_of=iso(START + timedelta(days=1))
        )
        assert sum(1 for landing in scored if landing.outlier) == 4

    def test_it_is_clamped(self, store: SqlTaskStore) -> None:
        start = history(store, 3, START)

        self.slow_landings(store, [3.0, 3.2, 2.8], start)
        bias = fe.learn_bias(store.read_connection(), store.project_id, START + timedelta(days=1))

        assert bias.factor == fe.BIAS_CEILING
        assert bias.learned == pytest.approx(3.0)
        assert bias.clamped is True

    def test_a_reset_forgets_it_and_deletes_nothing(self, store: SqlTaskStore) -> None:
        start = history(store, 3, START)
        self.slow_landings(store, [1.5, 1.5, 1.5], start)
        later = START + timedelta(days=1)

        store.reset_finish_estimator(later)
        bias = fe.learn_bias(store.read_connection(), store.project_id, later)

        assert (bias.factor, bias.sample, bias.reset_at) == (1.0, 0, iso(later))
        assert len(fe.measured(store.read_connection(), store.project_id, as_of=iso(later))) == 3


class TestTheAccuracyReport:
    def test_analytics_scores_each_week_against_the_eta_given_at_the_gate(
        self, store: SqlTaskStore
    ) -> None:
        # Within the hour: the page's window starts no earlier than the project's first
        # task, and these tasks were created just now.
        now = datetime.now(timezone.utc) + timedelta(hours=2)
        start = history(store, 3, now - timedelta(hours=2))
        TestTheCorrection().slow_landings(store, [1.1, 1.1, 1.1], start)

        payload = build_analytics(store, "30d", now=now)

        measured = [point for point in payload["estimates"] if point["sample"]]
        assert sum(point["sample"] for point in measured) == 3
        point = measured[-1]
        assert point["raw_error_p50_pct"] == pytest.approx(9.1, abs=0.1)  # 1/1.1 off by 9%
        assert point["within_20"] == 1.0
        assert payload["estimator"]["factor"] == pytest.approx(1.1)
        assert payload["estimator"]["active"] is True
        assert payload["estimates_coverage"]["recorded_from"] is not None


# ----- the surfaces ---------------------------------------------------------------------


@pytest.fixture()
def served(machine: Machine) -> Iterator[TestClient]:
    reset_dependency_cache()
    landing_estimate.forget()
    with TestClient(app) as client:
        yield client
    reset_dependency_cache()


def seeded_machine(machine: Machine, count: int = 5) -> None:
    storage = machine.manager.storage
    assert isinstance(storage, SqlTaskStore)
    history(storage, count, datetime.now(timezone.utc) - timedelta(days=2))


def row_estimate(client: TestClient, task_id: str) -> Optional[Dict[str, Any]]:
    rows = {row["id"]: row for row in client.get("/api/projects/sandbox/tasks").json()}
    finish = rows[task_id]["live_finish"]
    return finish["estimate"] if finish else None


def page_estimate(client: TestClient, task_id: str) -> Optional[Dict[str, Any]]:
    response = client.get(f"/api/projects/sandbox/dispatch/finishes/{task_id}")
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    estimate: Optional[Dict[str, Any]] = body["estimate"]
    return estimate


class TestTheSurfacesAgree:
    def test_the_row_and_the_page_carry_one_estimate(
        self, machine: Machine, served: TestClient
    ) -> None:
        seeded_machine(machine)
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)

        row = row_estimate(served, task_id)
        page = page_estimate(served, task_id)

        assert row is not None and row["kind"] == "estimate"
        assert row == page
        assert 0 < row["progress"] <= fe.PROGRESS_CAP
        assert row["eta_seconds"] > 0
        assert row["typical_seconds"] == TOTAL
        assert "Median of the last 5 finished landings" in row["basis"]

    def test_without_history_both_say_elapsed_time_only(
        self, machine: Machine, served: TestClient
    ) -> None:
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)

        row = row_estimate(served, task_id)

        assert row is not None and row["kind"] == "no_history"
        assert row["progress"] is None and row["eta_seconds"] is None
        assert row == page_estimate(served, task_id)

    def test_on_the_runway_both_are_indeterminate(
        self, machine: Machine, served: TestClient
    ) -> None:
        seeded_machine(machine)
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        directory = write_finish(machine.home, "fin_wait", task_id=task_id)
        record(directory, "finish_step", step="preflight", ok=True, seconds=1.0)
        hold_lock(machine.home, task_id, pid=os.getpid(), finish_id="fin_wait")

        row = row_estimate(served, task_id)

        assert row is not None and row["kind"] == "runway"
        assert row["progress"] is None and row["eta_seconds"] is None
        assert row == page_estimate(served, task_id)

    def test_a_finish_that_is_not_live_carries_none(
        self, machine: Machine, served: TestClient
    ) -> None:
        seeded_machine(machine)
        task_id = machine.task()
        write_finish(machine.home, "fin_done", task_id=task_id, outcome="finished")

        assert page_estimate(served, task_id) is None


class TestTheRoutes:
    def test_a_running_finish_written_over_http_records_a_prediction(
        self, machine: Machine, served: TestClient
    ) -> None:
        seeded_machine(machine)
        task_id = machine.task()
        response = served.put(
            "/api/projects/sandbox/history/finishes/fin_http",
            json={
                "record": {
                    "task_id": task_id,
                    "started_at": iso(datetime.now(timezone.utc)),
                    "outcome": "running",
                },
                "steps": [],
            },
        )
        assert response.status_code == 200, response.text

        storage = machine.manager.storage
        assert isinstance(storage, SqlTaskStore)
        rows = (
            storage.read_connection()
            .execute(
                "SELECT checkpoint, raw_eta_s FROM finish_prediction WHERE finish_id = 'fin_http'"
            )
            .fetchall()
        )
        assert [(row["checkpoint"], row["raw_eta_s"]) for row in rows] == [
            ("step:preflight", TOTAL)
        ]

    def test_the_owner_can_reset_the_correction(self, machine: Machine, served: TestClient) -> None:
        response = served.post("/api/projects/sandbox/analytics/finish-estimator/reset")

        assert response.status_code == 200, response.text
        storage = machine.manager.storage
        assert isinstance(storage, SqlTaskStore)
        assert fe.reset_at(storage.read_connection(), "sandbox") == response.json()["reset_at"]
