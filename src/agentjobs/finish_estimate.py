"""How far along a landing is, and roughly when it will be done (task-586).

A task in the **Landing** state has a scripted finish running against its branch:
preflight, runway, rebase, the full gate, merge, rebuild, restart, verify, close and
clean-up. That takes minutes, and the dashboard showed a chip and the task page a step
table. This module turns the finish's position and the project's own history into two
numbers -- a progress fraction and a time remaining -- plus the sentence that says what
they rest on.

**One computation, two surfaces.** :func:`estimate` is the only place the model lives.
The task list's ``live_finish`` (:mod:`agentjobs.api.live_finish`), the task page's
``GET /dispatch/finishes/{task_id}`` and the slot board all call
:func:`estimate_status` on the same :class:`~agentjobs.dispatch.finish_status.FinishStatus`
the liveness rule already produced, so the bar on a row and the bar on the page cannot
disagree. No client re-derives any of it.

**Weighted by time, not by step count.** The gate is about 90% of a landing. Counting
twelve steps equally would park the bar at a quarter for most of the wait, so each step
is weighted by its recent median duration, and inside the gate each stage by *its*
median from the gate history -- which is what makes the bar move during the long part.

Three things it will not do, each a way an estimate lies:

- **Invent a runway wait.** The merge runway is a lock that waiters race for, not a
  queue (task-572), so how long a finish waits for it cannot be read off history. On
  ``runway`` the answer is :data:`RUNWAY`: no progress, no ETA, and a sentence saying so.
- **Claim a basis it lacks.** Fewer than :data:`MIN_HISTORY` finished landings and the
  answer is :data:`NO_HISTORY`, which a surface renders as elapsed time only.
- **Say it is done before it is.** Progress is capped at :data:`PROGRESS_CAP` while the
  finish runs, and a landing past its whole expected duration is ``overrun`` -- "taking
  longer than usual" -- rather than a negative number or a bar that stops.

**It measures itself and corrects itself.** At every step start and every gate stage
start the server records the time remaining it predicted
(:func:`record_checkpoint`, called from the history routes the finisher and the gate
already write to). When a finish ends ``finished``, the actual remaining time at each
checkpoint is known, so each prediction's error is too. :func:`learn_bias` turns the
recent errors into one multiplicative factor -- a rolling median of actual over
predicted -- which catches what the medians miss systematically: the time between steps,
a catch-up after a busy base. The loop is guarded three ways, because a correction
nobody can see diverging is worse than none: the factor is clamped to
[:data:`BIAS_FLOOR`, :data:`BIAS_CEILING`]; finishes with a gate retry or a runway wait
are reported but do not teach it; and it can be reset, which makes only finishes that
start afterwards count. Its accuracy is reported on the analytics page
(``Analytics.estimates``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.finish_status import STEP_ORDER, FinishStatus
from agentjobs.history import moment_of, stamp

ESTIMATE = "estimate"
"""There is a basis: ``progress`` and ``eta_seconds`` are set."""
RUNWAY = "runway"
"""Waiting for the merge runway: indeterminate by design, never a number."""
NO_HISTORY = "no_history"
"""Too few finished landings in this project to say anything: elapsed time only."""

HISTORY_WINDOW = 20
"""How many recent finished landings the step medians are taken over.

Recent rather than all, so the medians follow drift -- a gate stage getting slower --
without anybody retuning anything."""

GATE_WINDOW = 20
"""How many recent green full gates the stage medians are taken over."""

MIN_HISTORY = 3
"""Fewer finished landings than this and there is no estimate at all."""

PROGRESS_CAP = 0.95
"""The most a running finish's bar may read. Only a recorded ending is 100%."""

BIAS_WINDOW = 20
"""How many recent measured landings the bias factor is the median over."""

BIAS_MIN_SAMPLE = 3
"""Fewer measured landings than this and the bias factor is 1: no correction."""

BIAS_FLOOR = 0.5
BIAS_CEILING = 2.0
"""The clamp. A factor at either edge is reported as clamped, which is the signal that
something other than ordinary drift is going on."""

MIN_RATIO_ETA_S = 60.0
"""A checkpoint predicting less than this is left out of the bias. Near the end of a
landing a two-second error is a ratio of two, which says nothing about the model."""

RUNWAY_WAIT_S = 5.0
"""A runway step longer than this is a wait for another finish, not the lock's own cost,
and such a landing does not teach the bias factor."""

ACCURACY_CHECKPOINT = "step:gate"
"""The prediction the accuracy report scores: the one given as the gate starts. The
runway is behind it by then, and the gate is most of what is left."""

ACCURACY_BAND = 0.2
"""A landing whose actual remaining time was within this fraction of the prediction
counts as "within 20%"."""


# ----- the model ------------------------------------------------------------------------


@dataclass(frozen=True)
class Bias:
    """The learned correction, and what it was learned from."""

    factor: float = 1.0
    sample: int = 0
    """Measured landings the factor is the median over."""
    learned: Optional[float] = None
    """The median before clamping; ``None`` below :data:`BIAS_MIN_SAMPLE`."""
    excluded: int = 0
    """Measured landings left out as outliers: a gate retry or a runway wait."""
    reset_at: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.sample >= BIAS_MIN_SAMPLE

    @property
    def clamped(self) -> bool:
        return self.learned is not None and self.learned != self.factor


@dataclass(frozen=True)
class EstimateModel:
    """What history says a landing costs, per step and per gate stage."""

    steps: Mapping[str, float] = field(default_factory=dict)
    """Median seconds from the previous step landing to this one -- so the time between
    steps is counted, and the steps sum to the landing."""
    stages: Tuple[Tuple[str, float], ...] = ()
    """Gate stages in the order they run, each with its median seconds."""
    sample: int = 0
    gate_sample: int = 0
    typical_s: Optional[float] = None
    """Median total seconds of the same finished landings."""
    bias: Bias = field(default_factory=Bias)

    def expected(self, step: str) -> float:
        return float(self.steps.get(step, 0.0))

    @property
    def total_s(self) -> float:
        """Expected seconds of a landing that waits for nothing."""
        return sum(self.expected(step) for step in STEP_ORDER if step != "runway")


@dataclass(frozen=True)
class Position:
    """Where one finish is, in the terms the model needs."""

    done: Tuple[Tuple[str, float], ...] = ()
    """Steps landed so far, in order, each with the seconds it actually took (measured
    landing to landing, the way the model's medians are)."""
    current: str = ""
    current_elapsed_s: float = 0.0
    stages_done: Tuple[str, ...] = ()
    stage: str = ""
    stage_elapsed_s: float = 0.0


@dataclass(frozen=True)
class Estimate:
    """The answer both surfaces render."""

    kind: str
    progress: Optional[float] = None
    eta_seconds: Optional[float] = None
    raw_eta_seconds: Optional[float] = None
    overrun: bool = False
    basis: str = ""
    typical_seconds: Optional[float] = None
    bias: float = 1.0
    elapsed_seconds: Optional[float] = None
    """How long the finish has been going, by the server's clock. Set by
    :func:`estimate_status`, so a row with no estimate can still say "4m so far"."""


def estimate(model: EstimateModel, position: Position) -> Estimate:
    """Progress and time remaining for a finish at ``position``. Pure; see the module."""
    typical = round(model.typical_s, 1) if model.typical_s is not None else None
    if model.sample < MIN_HISTORY or model.total_s <= 0:
        return Estimate(
            kind=NO_HISTORY,
            basis=(
                f"Fewer than {MIN_HISTORY} finished landings in this project's history, so "
                "there is nothing to estimate from: elapsed time only."
            ),
            typical_seconds=typical,
        )
    if position.current == "runway":
        return Estimate(
            kind=RUNWAY,
            basis=(
                "Waiting for the merge runway. Another landing holds it, and how long it "
                "keeps it cannot be told from history, so no time is estimated until it "
                "is free."
            ),
            typical_seconds=typical,
        )

    total = model.total_s
    done_names = {name for name, _ in position.done}
    done_expected = sum(model.expected(name) for name in done_names if name != "runway")
    credit, current_left = _current(model, position)

    try:
        at = STEP_ORDER.index(position.current) if position.current else len(STEP_ORDER)
    except ValueError:
        at = len(STEP_ORDER)
    later = sum(
        model.expected(step)
        for step in STEP_ORDER[at + 1 :]
        if step != "runway" and step not in done_names
    )
    raw_eta = current_left + later
    factor = model.bias.factor
    spent = sum(seconds for name, seconds in position.done if name != "runway")
    if position.current and position.current != "runway":
        spent += position.current_elapsed_s
    overrun = spent > total * factor
    progress = min(PROGRESS_CAP, max(0.0, (done_expected + credit) / total))
    return Estimate(
        kind=ESTIMATE,
        progress=round(progress, 4),
        eta_seconds=round(raw_eta * factor, 1),
        raw_eta_seconds=round(raw_eta, 1),
        overrun=overrun,
        basis=basis(model),
        typical_seconds=typical,
        bias=factor,
    )


def _current(model: EstimateModel, position: Position) -> Tuple[float, float]:
    """``(credit, left)`` for the step in flight: expected seconds done and to go.

    Inside the gate, when the gate has said which stage it is on, the gate's expected
    time is shared out over its stages by their medians -- so progress moves stage by
    stage instead of sitting still for five minutes and then jumping.
    """
    step = position.current
    if not step:
        return 0.0, 0.0
    expected = model.expected(step)
    staged = step == "gate" and model.stages and (position.stage or position.stages_done)
    if not staged:
        elapsed = position.current_elapsed_s
        return min(elapsed, expected), max(0.0, expected - elapsed)
    stage_total = sum(seconds for _, seconds in model.stages)
    if stage_total <= 0:
        return 0.0, expected
    scale = expected / stage_total
    done = set(position.stages_done)
    medians = dict(model.stages)
    finished = sum(seconds for name, seconds in model.stages if name in done)
    current = medians.get(position.stage, 0.0) if position.stage not in done else 0.0
    later = sum(
        seconds for name, seconds in model.stages if name not in done and name != position.stage
    )
    credit = scale * (finished + min(position.stage_elapsed_s, current))
    left = scale * (later + max(0.0, current - position.stage_elapsed_s))
    return min(credit, expected), left


def basis(model: EstimateModel) -> str:
    """One sentence on what an estimate rests on, for the task page's tooltip."""
    parts = [f"Median of the last {model.sample} finished landings"]
    if model.gate_sample:
        parts.append(f" and {model.gate_sample} green gates")
    bias = model.bias
    if bias.active:
        clamp = " (clamped)" if bias.clamped else ""
        parts.append(
            f", corrected ×{bias.factor:.2f}{clamp} from how far off the last "
            f"{bias.sample} were"
        )
    else:
        parts.append(
            f", not yet corrected: {bias.sample} of the {BIAS_MIN_SAMPLE} measured "
            "landings a correction needs"
        )
    parts.append(". An estimate, not a promise.")
    return "".join(parts)


# ----- where a live finish is ----------------------------------------------------------


def position_of(status: FinishStatus, now: Optional[datetime] = None) -> Position:
    """A live :class:`FinishStatus` as a :class:`Position`, measured against ``now``."""
    moment = now or dispatch_clock.utcnow()
    began = moment_of(status.started_at)
    previous = began
    done: List[Tuple[str, float]] = []
    for step in status.steps:
        if step.state == "running":
            continue
        landed = moment_of(step.landed_at)
        seconds = _span(previous, landed) if landed is not None else step.seconds
        done.append((step.name, seconds))
        if landed is not None:
            previous = landed
    current_elapsed = _span(previous, moment)
    gate = status.gate
    stages_done: Tuple[str, ...] = ()
    stage = ""
    stage_elapsed = 0.0
    if status.current_step == "gate" and gate is not None and gate.running:
        stages_done = tuple(gate.stages_done)
        stage = gate.stage
        stage_began = moment_of(gate.stage_started_at)
        stage_elapsed = _span(stage_began, moment) if stage_began is not None else 0.0
    return Position(
        done=tuple(done),
        current=status.current_step,
        current_elapsed_s=current_elapsed,
        stages_done=stages_done,
        stage=stage,
        stage_elapsed_s=stage_elapsed,
    )


def _span(start: Optional[datetime], end: Optional[datetime]) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def estimate_status(
    status: FinishStatus, model: Optional[EstimateModel], now: Optional[datetime] = None
) -> Optional[Estimate]:
    """The estimate for a live finish, or ``None`` for one that is not live.

    ``model`` is ``None`` when the caller could not read the store; that reads as no
    history rather than as an error, because a label may not cost the read it rides on.
    """
    if not status.live:
        return None
    answer = estimate(model or EstimateModel(), position_of(status, now))
    return replace(answer, elapsed_seconds=status.elapsed_seconds)


# ----- reading the model from the store ------------------------------------------------


def _as_of(now: Optional[datetime]) -> str:
    return stamp(now or dispatch_clock.utcnow()) or ""


def load_model(
    connection: sqlite3.Connection, project_id: str, now: Optional[datetime] = None
) -> EstimateModel:
    """The model as it stood at ``now``: only history that had ended by then counts.

    ``now`` is what makes a replay honest. The sandbox and the convergence test step a
    clock through a sequence of landings, and each prediction must be made from what was
    known at the time -- not from finishes that had not happened yet.

    A fixed number of statements whatever the history holds -- each read is one set
    query rather than one per finish -- for the reason the analytics page's query budget
    gives: a loop of reads is what grows unnoticed.
    """
    as_of = _as_of(now)
    recent = (
        "SELECT finish_id FROM finish WHERE project_id = ? AND outcome = 'finished' "
        "AND finished_at IS NOT NULL AND finished_at <= ? ORDER BY started_at DESC LIMIT ?"
    )
    finishes = connection.execute(
        "SELECT finish_id, started_at, seconds FROM finish "
        f"WHERE project_id = ? AND finish_id IN ({recent})",
        (project_id, project_id, as_of, HISTORY_WINDOW),
    ).fetchall()
    totals = [float(row["seconds"]) for row in finishes if row["seconds"] is not None]
    previous: Dict[str, Optional[datetime]] = {
        str(row["finish_id"]): moment_of(row["started_at"]) for row in finishes
    }
    per_step: Dict[str, List[float]] = {}
    for step in connection.execute(
        "SELECT finish_id, step, ts FROM finish_step "
        f"WHERE project_id = ? AND finish_id IN ({recent}) ORDER BY finish_id, seq",
        (project_id, project_id, as_of, HISTORY_WINDOW),
    ):
        finish_id = str(step["finish_id"])
        landed = moment_of(step["ts"])
        per_step.setdefault(str(step["step"]), []).append(_span(previous.get(finish_id), landed))
        if landed is not None:
            previous[finish_id] = landed
    steps = {name: float(median(values)) for name, values in per_step.items() if values}
    stages, gate_sample = _stage_medians(connection, project_id, as_of)
    return EstimateModel(
        steps=steps,
        stages=stages,
        sample=len(finishes),
        gate_sample=gate_sample,
        typical_s=float(median(totals)) if totals else None,
        bias=learn_bias(connection, project_id, now),
    )


def _stage_medians(
    connection: sqlite3.Connection, project_id: str, as_of: str
) -> Tuple[Tuple[Tuple[str, float], ...], int]:
    """Each gate stage's median over recent green full gates, in running order.

    A finish's own gates first, because that is the gate being estimated; any origin
    when there are too few of those, since a stage costs what it costs wherever it runs.
    """
    recent = (
        "SELECT gate_id FROM gate_run WHERE project_id = ? AND scope = 'full' "
        "AND passed = 1 AND finished_at IS NOT NULL AND finished_at <= ? {origin} "
        "ORDER BY started_at DESC LIMIT ?"
    )
    rows: List[sqlite3.Row] = []
    gates: Set[str] = set()
    for origin in ("AND origin = 'finish'", ""):
        rows = connection.execute(
            "SELECT gate_id, seq, stage, seconds FROM gate_stage WHERE project_id = ? "
            f"AND gate_id IN ({recent.format(origin=origin)})",
            (project_id, project_id, as_of, GATE_WINDOW),
        ).fetchall()
        gates = {str(row["gate_id"]) for row in rows}
        if len(gates) >= MIN_HISTORY:
            break
    seconds: Dict[str, List[float]] = {}
    order: Dict[str, List[int]] = {}
    for row in rows:
        if row["seconds"] is None:
            continue
        seconds.setdefault(str(row["stage"]), []).append(float(row["seconds"]))
        order.setdefault(str(row["stage"]), []).append(int(row["seq"]))
    ranked = sorted(seconds, key=lambda name: (median(order[name]), name))
    return tuple((name, float(median(seconds[name]))) for name in ranked), len(gates)


# ----- self-measurement ----------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    """One finished landing's predictions, scored against what happened."""

    finish_id: str
    started_at: str
    outlier: bool
    ratios: Tuple[float, ...]
    """actual / raw predicted, per checkpoint worth scoring."""
    at_gate: Optional[Tuple[float, float, float, float]] = None
    """``(eta_s, raw_eta_s, actual_s, bias)`` at :data:`ACCURACY_CHECKPOINT`."""


def reset_at(connection: sqlite3.Connection, project_id: str) -> Optional[str]:
    row = connection.execute(
        "SELECT reset_at FROM finish_estimator WHERE project_id = ?", (project_id,)
    ).fetchone()
    return str(row["reset_at"]) if row is not None and row["reset_at"] else None


_MEASURED = (
    "FROM finish f WHERE f.project_id = ? AND f.outcome = 'finished' "
    "AND f.finished_at IS NOT NULL AND f.finished_at <= ? "
    "AND EXISTS (SELECT 1 FROM finish_prediction p "
    "WHERE p.project_id = f.project_id AND p.finish_id = f.finish_id)"
)
"""The finished landings that recorded a prediction, up to a moment. Every read below is
scoped by it, so each is one statement over the set rather than one per landing."""


def measured(connection: sqlite3.Connection, project_id: str, *, as_of: str) -> List[Measured]:
    """Finished landings with recorded predictions, newest first, each scored.

    Only ``finished`` rows: a stopped or escalated finish is not a sample of how long
    landing takes, the rule the finish charts already apply. Four statements, however
    many landings there are.
    """
    scope = (project_id, as_of)
    finishes = connection.execute(
        f"SELECT f.finish_id, f.started_at, f.finished_at {_MEASURED} ORDER BY f.started_at DESC",
        scope,
    ).fetchall()
    if not finishes:
        return []
    ended = {str(row["finish_id"]): moment_of(row["finished_at"]) for row in finishes}
    ratios: Dict[str, List[float]] = {}
    at_gate: Dict[str, Tuple[float, float, float, float]] = {}
    for prediction in connection.execute(
        "SELECT finish_id, checkpoint, predicted_at, raw_eta_s, eta_s, bias "
        "FROM finish_prediction WHERE project_id = ? "
        f"AND finish_id IN (SELECT f.finish_id {_MEASURED})",
        (project_id, *scope),
    ):
        finish_id = str(prediction["finish_id"])
        actual = _span(moment_of(prediction["predicted_at"]), ended.get(finish_id))
        raw = float(prediction["raw_eta_s"])
        if raw >= MIN_RATIO_ETA_S and actual > 0:
            ratios.setdefault(finish_id, []).append(actual / raw)
        if prediction["checkpoint"] == ACCURACY_CHECKPOINT:
            at_gate[finish_id] = (
                float(prediction["eta_s"]),
                raw,
                actual,
                float(prediction["bias"]),
            )
    outliers = _outliers(connection, project_id, scope)
    return [
        Measured(
            finish_id=str(row["finish_id"]),
            started_at=str(row["started_at"]),
            outlier=str(row["finish_id"]) in outliers,
            ratios=tuple(ratios.get(str(row["finish_id"]), ())),
            at_gate=at_gate.get(str(row["finish_id"])),
        )
        for row in finishes
    ]


def _outliers(connection: sqlite3.Connection, project_id: str, scope: Tuple[str, str]) -> Set[str]:
    """Landings with a gate retry or a runway wait: reported, but they do not teach the bias."""
    found = {
        str(row["finish_id"])
        for row in connection.execute(
            "SELECT finish_id FROM gate_run WHERE project_id = ? "
            f"AND finish_id IN (SELECT f.finish_id {_MEASURED}) GROUP BY finish_id "
            "HAVING COUNT(*) > 1 OR SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) > 0",
            (project_id, *scope),
        )
    }
    found.update(
        str(row["finish_id"])
        for row in connection.execute(
            "SELECT finish_id FROM finish_step WHERE project_id = ? AND step = 'runway' "
            f"AND seconds > ? AND finish_id IN (SELECT f.finish_id {_MEASURED})",
            (project_id, RUNWAY_WAIT_S, *scope),
        )
    )
    return found


def learn_bias(
    connection: sqlite3.Connection,
    project_id: str,
    now: Optional[datetime] = None,
    *,
    landings: Optional[Sequence[Measured]] = None,
) -> Bias:
    """The bias factor as it stood at ``now``. See the module docstring.

    ``landings`` is :func:`measured` at ``now`` when the caller has already read it, so
    the analytics page pays for that read once.
    """
    since = reset_at(connection, project_id)
    if landings is None:
        landings = measured(connection, project_id, as_of=_as_of(now))
    samples: List[float] = []
    excluded = 0
    for landing in landings:
        if len(samples) >= BIAS_WINDOW:
            break
        if since and landing.started_at <= since:
            break
        if landing.outlier:
            excluded += 1
            continue
        if landing.ratios:
            samples.append(float(median(landing.ratios)))
    if len(samples) < BIAS_MIN_SAMPLE:
        return Bias(sample=len(samples), excluded=excluded, reset_at=since)
    learned = float(median(samples))
    factor = min(BIAS_CEILING, max(BIAS_FLOOR, learned))
    return Bias(
        factor=round(factor, 4),
        sample=len(samples),
        learned=round(learned, 4),
        excluded=excluded,
        reset_at=since,
    )


def reset(connection: sqlite3.Connection, project_id: str, now: Optional[datetime] = None) -> str:
    """Forget the learned correction: only landings that start after now teach it."""
    at = _as_of(now)
    connection.execute(
        "INSERT INTO finish_estimator(project_id, reset_at) VALUES (?, ?) "
        "ON CONFLICT(project_id) DO UPDATE SET reset_at = excluded.reset_at",
        (project_id, at),
    )
    return at


# ----- recording a prediction at a checkpoint ------------------------------------------


@dataclass(frozen=True)
class Checkpoint:
    """One recorded prediction."""

    finish_id: str
    checkpoint: str
    predicted_at: str
    elapsed_s: float
    raw_eta_s: float
    bias: float
    eta_s: float


def record_checkpoint(
    connection: sqlite3.Connection,
    project_id: str,
    finish_id: str,
    now: Optional[datetime] = None,
) -> Optional[Checkpoint]:
    """Write down what this running finish is predicted to need, if it is at a checkpoint.

    Called after every history write the finisher and the gate make. A finish write
    lands when a step does, which is the next step starting; a gate write lands when a
    stage does, which is the next stage starting. So the position read back from the
    store *is* a checkpoint, and the first prediction for it is the one kept -- a later
    write for the same position is ignored rather than moving the prediction closer to
    the answer.

    Nothing is written for a finish that has ended, one whose last step stopped, one on
    the runway (nothing is predicted there) or a project without enough history.
    """
    moment = now or dispatch_clock.utcnow()
    finish = connection.execute(
        "SELECT started_at, outcome FROM finish WHERE project_id = ? AND finish_id = ?",
        (project_id, finish_id),
    ).fetchone()
    if finish is None or finish["outcome"] != "running":
        return None
    began = moment_of(finish["started_at"])
    if began is None:
        return None
    previous = began
    done: List[Tuple[str, float]] = []
    last_ok = True
    for row in connection.execute(
        "SELECT step, ok, skipped, ts FROM finish_step WHERE project_id = ? AND finish_id = ? "
        "ORDER BY seq",
        (project_id, finish_id),
    ):
        landed = moment_of(row["ts"])
        done.append((str(row["step"]), _span(previous, landed)))
        if landed is not None:
            previous = landed
        last_ok = bool(row["ok"]) or bool(row["skipped"])
    if not last_ok:
        return None
    current = _after([name for name, _ in done])
    if not current or current == "runway":
        return None
    stages_done: Tuple[str, ...] = ()
    stage = ""
    model = load_model(connection, project_id, moment)
    if current == "gate":
        stages_done, stage = _gate_position(connection, project_id, finish_id, model)
    checkpoint = f"stage:{stage}" if stage else f"step:{current}"
    position = Position(
        done=tuple(done),
        current=current,
        current_elapsed_s=_span(previous, moment),
        stages_done=stages_done,
        stage=stage,
        stage_elapsed_s=0.0,
    )
    answer = estimate(model, position)
    if answer.kind != ESTIMATE or answer.raw_eta_seconds is None or answer.eta_seconds is None:
        return None
    recorded = Checkpoint(
        finish_id=finish_id,
        checkpoint=checkpoint,
        predicted_at=stamp(moment) or "",
        elapsed_s=round(_span(began, moment), 1),
        raw_eta_s=answer.raw_eta_seconds,
        bias=answer.bias,
        eta_s=answer.eta_seconds,
    )
    written = connection.execute(
        "INSERT OR IGNORE INTO finish_prediction(project_id, finish_id, checkpoint, "
        "predicted_at, elapsed_s, raw_eta_s, bias, eta_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            finish_id,
            recorded.checkpoint,
            recorded.predicted_at,
            recorded.elapsed_s,
            recorded.raw_eta_s,
            recorded.bias,
            recorded.eta_s,
        ),
    )
    return recorded if written.rowcount else None


def _after(landed: Sequence[str]) -> str:
    """The step after the last one landed, in the finish's fixed order."""
    if not landed:
        return STEP_ORDER[0]
    try:
        index = STEP_ORDER.index(landed[-1])
    except ValueError:
        return ""
    return STEP_ORDER[index + 1] if index + 1 < len(STEP_ORDER) else ""


def _gate_position(
    connection: sqlite3.Connection, project_id: str, finish_id: str, model: EstimateModel
) -> Tuple[Tuple[str, ...], str]:
    """``(stages done, stage starting now)`` of this finish's running gate, from its rows."""
    gate = connection.execute(
        "SELECT gate_id, finished_at FROM gate_run WHERE project_id = ? AND finish_id = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (project_id, finish_id),
    ).fetchone()
    if gate is None or gate["finished_at"]:
        return (), ""
    done = tuple(
        str(row["stage"])
        for row in connection.execute(
            "SELECT stage FROM gate_stage WHERE project_id = ? AND gate_id = ? "
            "AND seconds IS NOT NULL ORDER BY seq",
            (project_id, gate["gate_id"]),
        )
    )
    upcoming = [name for name, _ in model.stages if name not in done]
    return done, (upcoming[0] if upcoming else "")


# ----- the accuracy report -------------------------------------------------------------


def scored(landings: Iterable[Measured]) -> Dict[str, Any]:
    """Median absolute error and share within ±20%, for the analytics page."""
    errors: List[float] = []
    raw_errors: List[float] = []
    biases: List[float] = []
    within = 0
    outliers = 0
    for landing in landings:
        if landing.at_gate is None:
            continue
        eta, raw, actual, bias = landing.at_gate
        if actual <= 0:
            continue
        error = abs(eta - actual) / actual
        errors.append(error)
        raw_errors.append(abs(raw - actual) / actual)
        biases.append(bias)
        within += 1 if error <= ACCURACY_BAND else 0
        outliers += 1 if landing.outlier else 0
    sample = len(errors)
    return {
        "sample": sample,
        "error_p50_pct": round(median(errors) * 100, 1) if errors else None,
        "raw_error_p50_pct": round(median(raw_errors) * 100, 1) if raw_errors else None,
        "within_20": round(within / sample, 3) if sample else None,
        "bias_p50": round(float(median(biases)), 3) if biases else None,
        "outliers": outliers,
    }


__all__ = [
    "ACCURACY_BAND",
    "ACCURACY_CHECKPOINT",
    "BIAS_CEILING",
    "BIAS_FLOOR",
    "BIAS_MIN_SAMPLE",
    "ESTIMATE",
    "MIN_HISTORY",
    "NO_HISTORY",
    "PROGRESS_CAP",
    "RUNWAY",
    "Bias",
    "Checkpoint",
    "Estimate",
    "EstimateModel",
    "Measured",
    "Position",
    "estimate",
    "estimate_status",
    "learn_bias",
    "load_model",
    "measured",
    "position_of",
    "record_checkpoint",
    "reset",
    "reset_at",
    "scored",
]
