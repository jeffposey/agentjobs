"""The loop driver: spend an authorised chain, and stop loudly whatever ends it (task-150).

One function does the work -- :func:`run_chain` -- and it blocks until the chain is over,
for the reason ``epic.walk_epic`` blocks: *a supervisor that ends its turn promising to
check back is asleep*. Everything else here is the decision table, written out so that
each row can be read beside the sentence it writes onto the record.

**What one iteration is.**

1.  Re-read the task and resolve the authorisation **from it**. Not from memory, not from
    an argument. The bounds a person agreed to are a row in an append-only log, and
    reading them again every turn is what makes revocation take effect and what makes a
    restarted driver honest.
2.  Re-check every gate a dispatch passes, including the kill switch. An iteration is a
    dispatch; there is no weaker path.
3.  Dispatch, and **wait for that run to reach a terminal state in the ledger.** Never
    start *n+1* while *n* is live. The design's own argument for the outer loop says an
    iteration is worth starting only once the inner loop has genuinely stopped -- so this
    is not merely a safety rule, it is the thing that stops the loop paying to forget.
4.  Evaluate with task-147's evaluator and write **one** ``check_result`` carrying the
    whole vector, tagged with the chain and the iteration.
5.  Decide, in the order of the design's table.

**Every stop is loud, and that is enforced in one place.** :func:`_stop` is the only exit
from the loop: it hands the ball to a human, names the guardrail, the iteration and the
vector, and returns. A chain that stopped silently would be indistinguishable from one
still running, which is the failure the whole dispatch subsystem is built against.

**The loop never closes a task and never marks an unchecked criterion met** (L6). What it
establishes is that the machine-checkable half of the definition of done now holds; the
prose half is precisely the part that required taste, which is why it was prose. So a
converged chain hands off ``human``/``review`` naming both halves, and ``outcome:
completed`` is a word this module does not contain.

**Between iterations the driver moves the ball back itself.** A session that finished its
turn has handed the task to a human for review, so nothing would dispatch it again. The
driver writes an ``agent``/``revise`` handoff whose prompt is the failing vector -- which
is the resumption contract applied to a machine, and the only thing the next iteration's
fresh session gets to start from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Tuple

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.budget import DISPATCHER_ACTOR
from agentjobs.dispatch.chains import (
    ChainAuthorization,
    check_digest,
    current_chain,
    iteration_results,
    settled_criteria,
    unchecked_criteria,
)
from agentjobs.dispatch.checks import CheckReport, NoChecksError, evaluate_task
from agentjobs.dispatch.config import DispatchError, assert_dispatch_permitted
from agentjobs.dispatch.guards import (
    TERMINAL_RUN_STATUSES,
    DispatchRefused,
    DispatchRequest,
    dispatch_task,
    resolve_machine_home,
)
from agentjobs.dispatch.ledger import LedgerError, find_run
from agentjobs.models_v2 import (
    AcceptanceStatus,
    Ball,
    BallReason,
    CheckOutcome,
    DispatchTrigger,
    Task,
)
from agentjobs.projects import Project

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from agentjobs.manager import TaskManager

__all__ = [
    "ChainResult",
    "ChainStop",
    "IterationRecord",
    "POLL_SECONDS",
    "THRASH_REPEATS",
    "run_chain",
    "vector_of",
]

POLL_SECONDS = 15.0
"""How often the driver looks at the ledger while an iteration runs.

The epic walk's interval, for the same reason: an iteration lasts minutes at least, and a
tighter poll buys nothing but directory scans. It is a parameter on
:func:`run_chain` because the tests drive whole chains and cannot wait fifteen seconds a
turn.
"""

THRASH_REPEATS = 3
"""Identical consecutive vectors that stop a chain.

**Three, not two**, and the design argues the number rather than picking it: two identical
vectors is one flaky rerun plus one no-op fix, which happens routinely and is not yet
evidence of anything. Three is a pattern.
"""


# ----- what ended it ----------------------------------------------------------


class ChainStop(Enum):
    """Why a chain stopped. Every member writes a different sentence onto the record."""

    CONVERGED = "converged"
    """Every checked criterion passes. The one stop that is good news."""

    REGRESSION = "regression"
    """A criterion that was met is failed. Stops immediately, with no retry."""

    THRASH = "thrash"
    """Three consecutive identical result vectors."""

    ITERATION_CAP = "iteration_cap"
    """The authorisation's iteration count is spent."""

    WALL_CLOCK = "wall_clock"
    """The authorisation's wall-clock bound has expired."""

    CHECKS_CHANGED = "checks_changed"
    """The digest moved: the definition of done was edited after a human agreed to it."""

    REVOKED = "revoked"
    """Somebody stopped it, or the machine's kill switch did."""

    DISPATCH_REFUSED = "dispatch_refused"
    """A gate or a budget cap refused an iteration. The refusal names itself."""

    RUN_DID_NOT_SETTLE = "run_did_not_settle"
    """An iteration's run never reached a terminal state inside the chain's bound."""

    EVALUATION_FAILED = "evaluation_failed"
    """The checks could not be evaluated at all -- the task lost them, or a gate closed."""

    @property
    def is_success(self) -> bool:
        """Only convergence. Everything else is a guardrail, and none of them is good."""
        return self is ChainStop.CONVERGED


#: The ball reason each stop hands over under. Convergence is the only ``review``: there
#: is work to judge. Everything else is a call somebody has to make about a loop that did
#: not do what it was authorised to do, which is a ``decision``.
_STOP_REASONS: Dict[ChainStop, BallReason] = {
    ChainStop.CONVERGED: BallReason.REVIEW,
}


@dataclass(frozen=True)
class IterationRecord:
    """One turn: what was started, what it cost, and what the checks said afterwards."""

    iteration: int
    run_id: Optional[str]
    vector: Tuple[Tuple[str, str], ...]
    report: Optional[CheckReport] = None
    detail: str = ""


@dataclass
class ChainResult:
    """What a chain did, from its first iteration to the sentence that ended it."""

    task_id: str
    chain_id: str
    stop: ChainStop
    detail: str
    iterations: List[IterationRecord] = field(default_factory=list)
    ball_prompt: str = ""

    @property
    def iterations_run(self) -> int:
        """Iterations that actually dispatched something. Zero is a legitimate answer."""
        return len([item for item in self.iterations if item.run_id is not None])

    def summary(self) -> str:
        """One line for a CLI, a log body or a test assertion."""
        return (
            f"{self.task_id} chain `{self.chain_id}` stopped: {self.stop.value} after "
            f"{self.iterations_run} iteration(s). {self.detail}"
        )


# ----- the vector -------------------------------------------------------------


def vector_of(results: Sequence[CheckOutcome]) -> Tuple[Tuple[str, str], ...]:
    """A pass's result vector: ``(criterion id, status)`` in the task's own order.

    **The comparison unit for both guardrails, and it is deliberately not the diff.** An
    agent that edits files busily while every check keeps returning the same answer is
    thrashing, and looking at the diff would call that progress. Exit codes and output
    tails are excluded for the same reason from the other side: a test that fails on a
    different line is still failing, and calling that a new vector would make thrash
    detection unreachable for any suite that prints a timestamp.
    """
    return tuple((outcome.id, outcome.status.value) for outcome in results)


def _regressed(
    previous: Sequence[Tuple[str, str]], current: Sequence[Tuple[str, str]]
) -> List[str]:
    """Criteria that were ``met`` in ``previous`` and are ``failed`` in ``current``.

    Compared against the immediately preceding vector rather than against the best the
    chain ever managed. That is the design's own wording -- *was met and becomes failed* --
    and it is also the reading that gives a stable rule: a best-ever baseline would keep
    re-firing on a criterion that flapped once, long after the iteration that broke it.
    """
    was_met = {name for name, status in previous if status == AcceptanceStatus.MET.value}
    return [
        name
        for name, status in current
        if name in was_met and status == AcceptanceStatus.FAILED.value
    ]


def _thrashing(vectors: Sequence[Tuple[Tuple[str, str], ...]]) -> bool:
    """Whether the last :data:`THRASH_REPEATS` vectors are all identical."""
    if len(vectors) < THRASH_REPEATS:
        return False
    recent = vectors[-THRASH_REPEATS:]
    return all(item == recent[0] for item in recent[1:])


def _render_vector(vector: Sequence[Tuple[str, str]]) -> str:
    """The vector as a person reads it in a ball prompt."""
    if not vector:
        return "no checked criteria"
    return ", ".join(f"{name}: {status}" for name, status in vector)


# ----- the driver -------------------------------------------------------------


def run_chain(
    *,
    manager: "TaskManager",
    project: Project,
    project_config: Dict[str, object],
    task_id: str,
    home: Optional[Path] = None,
    api_base: Optional[str] = None,
    poll_seconds: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Optional[Callable[[], datetime]] = None,
) -> ChainResult:
    """Spend the chain authorised against ``task_id``, and say what stopped it.

    Blocks until the chain is over. ``poll_seconds`` and ``sleep`` are the test seams --
    a suite that drove real chains at the production interval would take an hour -- and
    every caller in the application leaves them alone.

    Raises nothing for a chain that fails: every ending is a :class:`ChainResult` with a
    :class:`ChainStop` on it, and every one of them has already been written onto the task
    with the ball on a human. A caller that wants to know whether it went well asks
    ``result.stop.is_success``; a caller that wants to know whether *anything* went wrong
    reads the record, which is the point.

    The one thing this does raise is a missing task, because that is a wiring mistake
    rather than an outcome.
    """
    clock = now or dispatch_clock.utcnow

    task = manager.get_task(task_id)
    if task is None:
        raise ValueError(f"No task {task_id!r} in project {project.id!r}, so no chain to run.")

    chain = current_chain(task)
    if chain is None:
        # Nothing to stop loudly: there is no chain, so there is nobody who thinks one is
        # running. This is the only exit that writes nothing, and it is not a guardrail
        # tripping -- it is the driver being asked to spend an authorisation that does
        # not exist.
        return ChainResult(
            task_id=task_id,
            chain_id="",
            stop=ChainStop.REVOKED,
            detail=(
                f"{task_id} has no live chain: none was authorised, or the one that was "
                "has been revoked or has expired. Nothing was started."
            ),
        )

    state = _Driver(
        manager=manager,
        project=project,
        project_config=project_config,
        chain=chain,
        home=home,
        api_base=api_base,
        poll_seconds=poll_seconds,
        sleep=sleep,
        clock=clock,
    )
    return state.run(task)


@dataclass
class _Driver:
    """One chain in flight. Holds the loop's state so the decision table stays readable."""

    manager: "TaskManager"
    project: Project
    project_config: Dict[str, object]
    chain: ChainAuthorization
    home: Optional[Path]
    api_base: Optional[str]
    poll_seconds: float
    sleep: Callable[[float], None]
    clock: Callable[[], datetime]
    iterations: List[IterationRecord] = field(default_factory=list)
    vectors: List[Tuple[Tuple[str, str], ...]] = field(default_factory=list)

    # ----- the loop -----------------------------------------------------------

    def run(self, task: Task) -> ChainResult:
        """Iterate until a guardrail, a ceiling or convergence stops the chain."""
        # Iteration zero is the baseline the authorisation recorded. Seeding the vector
        # history with it is what lets the regression guard fire on iteration one: a
        # criterion that passed when the human agreed to the chain and fails after the
        # first run is exactly the case the guard exists for, and a history that started
        # at one would miss it.
        for _, recorded in iteration_results(task, self.chain.chain_id):
            self.vectors.append(tuple(recorded))

        while True:
            task = self._reread(task)
            halt = self._guardrails_before_dispatch(task)
            if halt is not None:
                return self._stop(task, *halt)

            iteration = len(self.iterations) + 1
            started = self._dispatch(task, iteration)
            if isinstance(started, tuple):
                return self._stop(task, *started)

            run_id = started
            settled = self._await_terminal(run_id)
            if settled is not None:
                self.iterations.append(
                    IterationRecord(iteration=iteration, run_id=run_id, vector=(), detail=settled)
                )
                return self._stop(
                    self._reread(task), ChainStop.RUN_DID_NOT_SETTLE, settled
                )

            task = self._reread(task)
            evaluated = self._evaluate(task, iteration, run_id)
            if isinstance(evaluated, tuple):
                return self._stop(task, *evaluated)

            report = evaluated
            vector = vector_of(report.results)
            task = self.manager.record_check_result(
                task.id,
                actor=DISPATCHER_ACTOR,
                results=report.results,
                unchecked=report.unchecked,
                chain_id=self.chain.chain_id,
                iteration=iteration,
                body=(
                    f"Iteration {iteration} of {self.chain.data.max_iterations}: "
                    f"{len(report.results) - len(report.failed)} of "
                    f"{len(report.results)} checks pass."
                ),
            )
            self.iterations.append(
                IterationRecord(
                    iteration=iteration, run_id=run_id, vector=vector, report=report
                )
            )
            previous = self.vectors[-1] if self.vectors else ()
            self.vectors.append(vector)

            verdict = self._decide(iteration, previous, vector, report)
            if verdict is not None:
                return self._stop(task, *verdict)

            task = self._ask_for_another_turn(task, iteration, report)

    def _reread(self, task: Task) -> Task:
        """Re-read the record, rather than reasoning about what a write left behind.

        Every write in this module returns the task it produced, and every one of them is
        discarded in favour of this. The record is shared -- a person can revoke a chain,
        edit a check or close the task while an iteration runs -- and a driver holding a
        copy from before the run it just waited on would be deciding on a task that no
        longer exists.
        """
        fresh = self.manager.get_task(task.id)
        return fresh if fresh is not None else task

    # ----- the decision table -------------------------------------------------

    def _decide(
        self,
        iteration: int,
        previous: Sequence[Tuple[str, str]],
        vector: Tuple[Tuple[str, str], ...],
        report: CheckReport,
    ) -> Optional[Tuple[ChainStop, str]]:
        """The design's table, in its own order. ``None`` means iterate."""
        if report.ok:
            return (
                ChainStop.CONVERGED,
                f"Every checked criterion passed at iteration {iteration}.",
            )

        regressed = _regressed(previous, vector)
        if regressed:
            return (
                ChainStop.REGRESSION,
                (
                    f"{', '.join(regressed)} passed at the previous evaluation and fails "
                    f"at iteration {iteration}. Stopped immediately with no retry: going "
                    "backwards means the loop's model of what it is doing is wrong, and "
                    "the specific thing this catches is an agent breaking a passing "
                    "check to make a failing one pass."
                ),
            )

        if _thrashing(self.vectors):
            return (
                ChainStop.THRASH,
                (
                    f"The last {THRASH_REPEATS} evaluations produced an identical result "
                    f"vector ({_render_vector(vector)}). The comparison is on the vector "
                    "and not on the diff, so work was very likely done -- it just did "
                    "not move any check."
                ),
            )
        return None

    # ----- the gates, before anything is spent --------------------------------

    def _guardrails_before_dispatch(self, task: Task) -> Optional[Tuple[ChainStop, str]]:
        """Everything that has to hold before an iteration may start.

        Re-read every turn, in this order, and the order is what a reader should check
        against the design: revocation first because it is the kill switch and must not
        be reportable as anything else; then the two ceilings; then the digest, which is
        the one that says a human's agreement no longer describes what is there.
        """
        live = current_chain(task)
        if live is None or live.chain_id != self.chain.chain_id:
            return (
                ChainStop.REVOKED,
                (
                    f"Chain `{self.chain.chain_id}` is no longer the live authorisation "
                    f"on {task.id}: it was revoked, or its wall-clock ran out. No "
                    "further iteration was started."
                ),
            )

        if live.expired(self.clock()):
            hours = live.data.wall_clock_seconds / 3600
            return (
                ChainStop.WALL_CLOCK,
                (
                    f"The chain's {hours:.1f}h wall-clock bound expired at "
                    f"{live.deadline.isoformat()}. No further iteration was started."
                ),
            )

        if len(self.iterations) >= live.data.max_iterations:
            return (
                ChainStop.ITERATION_CAP,
                (
                    f"The chain's {live.data.max_iterations}-iteration cap is spent. A "
                    "chain that needs more than its cap is not converging, which is the "
                    "thing the cap is there to tell you."
                ),
            )

        current = check_digest(task)
        if current != live.data.check_digest:
            return (
                ChainStop.CHECKS_CHANGED,
                (
                    "The task's checks are no longer the ones this chain was authorised "
                    f"against: the digest was `{live.data.check_digest[:12]}` and is now "
                    f"`{current[:12]}`. The criteria the authorisation covered were "
                    f"{', '.join(live.data.criteria) or 'none'}. Somebody -- or "
                    "something -- moved the definition of done after a person agreed to "
                    "it, so the chain stopped rather than converging on the new one."
                ),
            )

        try:
            assert_dispatch_permitted(self.project.id, self.home)
        except DispatchError as exc:
            # The kill switch lands here. `~/.agentjobs/DISPATCH_DISABLED` is one of the
            # four gates this call walks, and it is re-read on every iteration rather
            # than resolved once at the top -- which is the whole of the claim that the
            # sentinel stops every chain on the machine at once.
            return (
                ChainStop.REVOKED,
                (
                    f"A dispatch gate refused this iteration ({getattr(exc, 'reason', 'dispatch_error')}): "
                    f"{exc}"
                ),
            )
        return None

    # ----- spending one iteration ---------------------------------------------

    def _dispatch(self, task: Task, iteration: int) -> "str | Tuple[ChainStop, str]":
        """Start iteration ``iteration``, or the stop its refusal amounts to.

        ``caused_by`` names the ``chain_authorized`` entry, which is the human act this
        run traces to -- so ``assert_human_clocked`` reads a real row written by a real
        person, exactly as it does for a click. It has to be named explicitly: by now the
        newest entry is the driver's own handoff, and a dispatch attributed to AgentJobs'
        own entry is refused as an agent's. Correctly.
        """
        try:
            handle = dispatch_task(
                manager=self.manager,
                project=self.project,
                project_config=self.project_config,
                request=DispatchRequest(
                    task_id=task.id,
                    trigger=DispatchTrigger.CHAIN,
                    caused_by=self.chain.entry.id,
                ),
                home=self.home,
                api_base=self.api_base,
            )
        except DispatchRefused as exc:
            self.iterations.append(
                IterationRecord(
                    iteration=iteration, run_id=None, vector=(), detail=str(exc)
                )
            )
            return (
                ChainStop.DISPATCH_REFUSED,
                (
                    f"Iteration {iteration} was refused before it started "
                    f"({getattr(exc, 'reason', 'dispatch_refused')}): {exc}"
                ),
            )
        except DispatchError as exc:
            self.iterations.append(
                IterationRecord(
                    iteration=iteration, run_id=None, vector=(), detail=str(exc)
                )
            )
            return (
                ChainStop.DISPATCH_REFUSED,
                f"Iteration {iteration} could not be started: {exc}",
            )
        return handle.run_id

    def _await_terminal(self, run_id: str) -> Optional[str]:
        """Block until ``run_id`` is terminal. ``None`` on success, a sentence on failure.

        **This is the rule that iteration n+1 never begins while n is live**, and it is
        asserted against the ledger rather than against a process: a run whose session is
        parked on a login refusal has a live process and is not finished, and a run whose
        worker died has no process and is. The ledger is the only thing that knows which.

        Bounded by the chain's own wall-clock. A run that never settles therefore cannot
        hold the driver past the bound a person agreed to, which is the one property that
        makes an unattended chain safe to start after dinner.
        """
        root = _machine_home(self.home, self.project.id)
        while True:
            try:
                record = find_run(root, run_id)
            except LedgerError as exc:
                return (
                    f"Run `{run_id}` disappeared from the ledger while the chain was "
                    f"waiting for it: {exc}"
                )
            if record.status in TERMINAL_RUN_STATUSES:
                return None
            if self.clock() >= self.chain.deadline:
                return (
                    f"Run `{run_id}` was still {record.status!r} when the chain's "
                    "wall-clock bound ran out. The chain stops rather than waiting for "
                    "morning; the run itself was left alone and is not cancelled by this."
                )
            self.sleep(self.poll_seconds)

    def _evaluate(
        self, task: Task, iteration: int, run_id: str
    ) -> "CheckReport | Tuple[ChainStop, str]":
        """Run the checks, or the stop that not being able to run them amounts to."""
        try:
            return evaluate_task(task, project=self.project, home=self.home)
        except NoChecksError:
            return (
                ChainStop.EVALUATION_FAILED,
                (
                    f"After iteration {iteration} (run `{run_id}`) the task carries no "
                    "acceptance criterion with a `check` at all. The digest guard would "
                    "normally catch this; reaching here means the criteria were removed "
                    "between the two reads."
                ),
            )
        except DispatchError as exc:
            return (
                ChainStop.EVALUATION_FAILED,
                (
                    f"The checks could not be evaluated after iteration {iteration}: "
                    f"{exc}"
                ),
            )

    def _ask_for_another_turn(self, task: Task, iteration: int, report: CheckReport) -> Task:
        """Hand the ball back to an agent with the failing vector as the ask.

        Without this the chain stops by accident rather than by decision: the session
        that just finished handed the task to a human for review, and a dispatch of a
        task whose ball is not with the agent is refused. So the driver states the ask
        itself -- and the ask is the thing the next iteration's fresh session has instead
        of the last one's context, which is the outer loop's entire value proposition.
        """
        failing = ", ".join(outcome.id for outcome in report.failed)
        tails = [
            f"**{outcome.id}** (exit {outcome.exit_code if outcome.exit_code is not None else outcome.cause}):"
            f"\n\n```\n{outcome.output_tail}\n```"
            for outcome in report.failed
            if outcome.output_tail
        ]
        prompt = (
            f"Iteration {iteration + 1} of at most {self.chain.data.max_iterations} in "
            f"chain `{self.chain.chain_id}`.\n\n"
            f"Still failing: {failing}. Make those checks pass. Do not edit any "
            "criterion's `check` -- the chain is authorised against the set as it stood, "
            "and changing one stops the chain instead of moving the finish line.\n\n"
            "Hand off when your turn is done; the driver evaluates the checks itself and "
            "decides whether there is another turn."
        )
        if tails:
            prompt = prompt + "\n\n" + "\n\n".join(tails)
        return self.manager.handoff(
            task.id,
            actor=DISPATCHER_ACTOR,
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt=prompt,
            body=(
                f"Chain `{self.chain.chain_id}` iteration {iteration} evaluated: "
                f"{len(report.results) - len(report.failed)} of {len(report.results)} "
                f"checks pass. Asking for another turn on {failing}."
            ),
        )

    # ----- stopping, which is always loud -------------------------------------

    def _stop(self, task: Task, stop: ChainStop, detail: str) -> ChainResult:
        """The only exit from the loop. Hands the ball to a human and names the guardrail.

        One function, so "every stop is loud" is a property of the code rather than a
        rule thirteen branches have to remember. The handoff is best-effort in one narrow
        sense -- a task somebody closed under the driver cannot be handed to anyone -- and
        the result still carries the whole story for the caller.
        """
        fresh = self.manager.get_task(task.id) or task
        vector = self.iterations[-1].vector if self.iterations else ()
        settled = settled_criteria(
            fresh, self.iterations[-1].report.results if self.iterations and self.iterations[-1].report else []
        )
        untouched = unchecked_criteria(fresh)

        prompt = _stop_prompt(
            chain=self.chain,
            stop=stop,
            detail=detail,
            iteration=len(self.iterations),
            vector=vector,
            settled=settled,
            untouched=untouched,
        )
        result = ChainResult(
            task_id=fresh.id,
            chain_id=self.chain.chain_id,
            stop=stop,
            detail=detail,
            iterations=list(self.iterations),
            ball_prompt=prompt,
        )
        if fresh.is_open:
            self.manager.handoff(
                fresh.id,
                actor=DISPATCHER_ACTOR,
                ball=Ball.HUMAN,
                ball_reason=_STOP_REASONS.get(stop, BallReason.DECISION),
                ball_prompt=prompt,
                body=(
                    f"Chain `{self.chain.chain_id}` stopped after "
                    f"{result.iterations_run} iteration(s): {stop.value}.\n\n{detail}"
                ),
            )
        # Revoking the chain the driver just finished is deliberate and is not a
        # judgement about why it stopped. An authorisation nobody has withdrawn stays
        # live until its wall-clock expires, so a second driver -- or the same one,
        # restarted -- would read it and start iterating again on a task a human is now
        # holding. Stopping is an event; the record has to show the authorisation is
        # spent as well as why.
        self.manager.record_chain_revocation(
            fresh.id,
            actor=DISPATCHER_ACTOR,
            chain_id=self.chain.chain_id,
            re=self.chain.entry.id,
            body=(
                f"Chain `{self.chain.chain_id}` is spent: it stopped as `{stop.value}` "
                f"after {result.iterations_run} iteration(s), and the ball is with a "
                "human. Recorded so nothing reads the authorisation as live again."
            ),
        )
        return result


def _stop_prompt(
    *,
    chain: ChainAuthorization,
    stop: ChainStop,
    detail: str,
    iteration: int,
    vector: Sequence[Tuple[str, str]],
    settled: Sequence[str],
    untouched: Sequence[str],
) -> str:
    """The ask a human reads when a chain stops. Names the guardrail, turn and vector.

    Convergence gets a different opening from everything else, because it is asking for a
    different judgement: *the checkable half holds, now judge the half that needed taste*.
    Every other stop is asking somebody to decide what went wrong with a loop.
    """
    heading = (
        f"Chain `{chain.chain_id}` converged at iteration {iteration}."
        if stop.is_success
        else f"Chain `{chain.chain_id}` stopped at iteration {iteration}: **{stop.value}**."
    )
    lines = [heading, "", detail, "", f"Result vector: {_render_vector(vector)}."]
    if stop.is_success:
        lines.extend(
            [
                "",
                f"The loop settled: {', '.join(settled) or 'nothing'}.",
                f"It never touched: {', '.join(untouched) or 'nothing -- every criterion has a check'}.",
                "",
                "The loop has established that the machine-checkable half of this task's "
                "definition of done now holds. It has not closed the task and will not: "
                "the prose criteria are the part that needed judgement, which is what it "
                "is asking you for.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"Criteria still unchecked by any command: {', '.join(untouched) or 'none'}.",
                "",
                "Nothing further will start on its own -- the authorisation is spent. "
                "Read the `check_result` entries above for the chain's whole history, "
                "then decide whether to fix the task, fix the checks, or authorise "
                "another chain.",
            ]
        )
    return "\n".join(lines)


def _machine_home(home: Optional[Path], project_id: str) -> Path:
    """The AgentJobs home runs land beside, resolved the way the guards resolve it."""
    resolution = assert_dispatch_permitted(project_id, home)
    return resolve_machine_home(home, resolution)
