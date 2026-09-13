"""The versioned, pure reducer: where an execution stands, computed only from its history.

Design section 9a asks for "a small explicit state machine", and this is it. Two rules
make it replayable, and a test holds each one:

* **It reads nothing but events.** No clock, no configuration, no roster, no filesystem.
  A timer firing, a poll result, a Stop, a policy check -- anything from the world arrives
  as an event an adapter appended, and a fresh process replaying the same events reaches
  the same state and proposes the same intents. That is what lets the server after a
  restart, a CLI invocation or a new poller resume an execution none of them started.
* **It is versioned, and an unknown version is refused, never guessed.** Replaying old
  events through whichever control flow happens to be installed is the failure DBOS's
  upgrade guidance names; ``HistoryIncompatible`` is raised instead, and the store refuses
  the mutation that would have followed.

Intents are proposals with **stable activity ids**. Replaying a history twice names the
same activities, so recording them is idempotent and a replay by itself performs nothing.
Carrying them out is an adapter's job, outside any transaction (``coordinator``).

**Version 2 (task-416)** adds bounded recovery between attempts. An attempt that ended
retryably leaves its execution in ``retry_wait``; from there the proposals are, in order:
schedule a durable timer, observe policy when it fires, relaunch when the recorded
observation permits it -- or escalate once, naming the single action that clears the
condition. Version 1 histories replay under the same rules: none of the events that drive
the new states can appear in them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from agentjobs.execution.errors import HistoryIncompatible

WORKFLOW_VERSION = 2
"""The workflow definition this reducer implements. Independent of the package version:
a release that changes no transition must not strand every history written before it."""

SUPPORTED_VERSIONS = frozenset({1, WORKFLOW_VERSION})

# The event vocabulary. Typed as constants rather than an Enum so a persisted kind is a
# plain string whose meaning is this table, and an unknown one is detectable.
ACCEPTED = "accepted"
ADMITTED = "admitted"
LAUNCHED = "launched"
OBSERVED = "observed"
SIGNAL = "signal"
STOP_REQUESTED = "stop_requested"
CONCLUDED = "concluded"
MIGRATED = "migrated"
STAND_DOWN_REQUESTED = "stand_down_requested"
"""An administrative transfer of the task to another owner (task-312). Not a Stop: it
revokes no intent and must never be read as a cancellation."""
SIGNAL_DISPOSED = "signal_disposed"
"""A pending signal reached its explicit disposition -- consumed, superseded or stale."""
# Version 2.
CLOSED = "closed"
TIMER_SET = "timer_set"
TIMER_FIRED = "timer_fired"
POLICY_OBSERVED = "policy_observed"
ACTIVITY_RESULT = "activity_result"

EVENT_KINDS = frozenset(
    {
        ACCEPTED,
        ADMITTED,
        LAUNCHED,
        OBSERVED,
        SIGNAL,
        STOP_REQUESTED,
        CONCLUDED,
        MIGRATED,
        STAND_DOWN_REQUESTED,
        SIGNAL_DISPOSED,
        CLOSED,
        TIMER_SET,
        TIMER_FIRED,
        POLICY_OBSERVED,
        ACTIVITY_RESULT,
    }
)

# Execution states (design section 9a's diagram). Not task lifecycle values.
S_NEW = "new"
S_ACCEPTED = "accepted"
S_LAUNCHING = "launching"
S_WORKING = "working"
S_RETRY_WAIT = "retry_wait"
S_PARKED = "parked"
S_STOPPING = "stopping"
S_STANDING_DOWN = "standing_down"
S_CANCELLED = "cancelled"
S_CONCLUDED = "concluded"

TERMINAL_STATES = frozenset({S_CANCELLED, S_CONCLUDED})

# ----- failure classes (design section 9a's table) --------------------------------

WORKER_GONE = "worker_gone"
LAUNCH_NOT_APPLIED = "launch_not_applied"
EFFECT_UNKNOWN = "effect_unknown"
CAPACITY_WAIT = "capacity_wait"
COOLDOWN = "cooldown"
POLICY_WAIT = "policy_wait"
POLICY_REVOKED = "policy_revoked"
BUDGET_EXHAUSTED = "budget_exhausted"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
WORKER_FAILED = "worker_failed"
TIMED_OUT = "timed_out"
SPEC_GAP = "spec_gap"
AUTH_UNAVAILABLE = "auth_unavailable"
STORAGE_FAILURE = "storage_failure"
CANCELLED_BY_USER = "cancelled_by_user"

RETRYABLE_CLASSES = frozenset({WORKER_GONE, LAUNCH_NOT_APPLIED})
"""Classes that may spend another paid attempt. Everything else is a fact about the work,
the grant or the world, and a second identical attempt would not change it -- in
particular a branch or code failure is never retried hoping for green."""

WAIT_CLASSES = frozenset({CAPACITY_WAIT, COOLDOWN, POLICY_WAIT})
"""Conditions that clear on their own. Waiting on them spends no attempt; another round
of the same wait is scheduled."""

DEFAULT_RETRY_POLICY: Mapping[str, Any] = {
    "max_attempts": 3,
    "initial_seconds": 60,
    "factor": 2,
    "cap_seconds": 600,
    "jitter_seconds": 15,
    "max_wait_rounds": 30,
}
"""What a new envelope is granted (``dispatch.envelope``). Three paid attempts per
execution; the first delay equals the default per-task cooldown so a relaunch is not
refused by it. Proposed policy, not a measurement of optimal latency (design section 9a).
An envelope that recorded no policy -- everything admitted before task-416 -- gets no
automatic retry at all rather than today's default."""


@dataclass(frozen=True)
class Event:
    """One persisted event, as the reducer sees it."""

    sequence: int
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_id: str = ""


@dataclass(frozen=True)
class Intent:
    """A proposed activity. ``activity_id`` is stable across replays of one history."""

    kind: str
    activity_id: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionState:
    """Everything the reducer knows. Serialisable, so it can be snapshotted."""

    execution_id: str
    version: int = WORKFLOW_VERSION
    state: str = S_NEW
    last_sequence: int = 0
    envelope: Mapping[str, Any] = field(default_factory=dict)
    owner_mode: str = "durable"
    attempt_no: int = 0
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    stop_generation: int = 0
    stop_requester: Optional[str] = None
    outcome: Optional[str] = None
    pending_signals: Tuple[str, ...] = ()
    observation: Optional[str] = None
    transfer_to: Optional[str] = None
    # Version 2.
    failure_class: Optional[str] = None
    wait_round: int = 0
    timer_id: Optional[str] = None
    timer_fired: bool = False
    policy: Optional[Mapping[str, Any]] = None
    escalated: Optional[str] = None
    attempt_live: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_data(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "version": self.version,
            "state": self.state,
            "last_sequence": self.last_sequence,
            "envelope": dict(self.envelope),
            "owner_mode": self.owner_mode,
            "attempt_no": self.attempt_no,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "stop_generation": self.stop_generation,
            "stop_requester": self.stop_requester,
            "outcome": self.outcome,
            "pending_signals": list(self.pending_signals),
            "observation": self.observation,
            "transfer_to": self.transfer_to,
            "failure_class": self.failure_class,
            "wait_round": self.wait_round,
            "timer_id": self.timer_id,
            "timer_fired": self.timer_fired,
            "policy": dict(self.policy) if self.policy is not None else None,
            "escalated": self.escalated,
            "attempt_live": self.attempt_live,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "ExecutionState":
        version = int(data.get("version", 0))
        _assert_supported(version)
        policy = data.get("policy")
        return cls(
            execution_id=str(data["execution_id"]),
            version=version,
            state=str(data["state"]),
            last_sequence=int(data["last_sequence"]),
            envelope=dict(data.get("envelope") or {}),
            owner_mode=str(data.get("owner_mode") or "durable"),
            attempt_no=int(data.get("attempt_no") or 0),
            run_id=data.get("run_id"),
            session_id=data.get("session_id"),
            stop_generation=int(data.get("stop_generation") or 0),
            stop_requester=data.get("stop_requester"),
            outcome=data.get("outcome"),
            pending_signals=tuple(data.get("pending_signals") or ()),
            observation=data.get("observation"),
            transfer_to=data.get("transfer_to"),
            failure_class=data.get("failure_class"),
            wait_round=int(data.get("wait_round") or 0),
            timer_id=data.get("timer_id"),
            timer_fired=bool(data.get("timer_fired")),
            policy=dict(policy) if isinstance(policy, Mapping) else None,
            escalated=data.get("escalated"),
            attempt_live=bool(data.get("attempt_live")),
        )


def _assert_supported(version: int) -> None:
    if version not in SUPPORTED_VERSIONS:
        raise HistoryIncompatible(
            f"this history was written by workflow version {version}; this build replays "
            f"only {sorted(SUPPORTED_VERSIONS)}. It is kept, and not acted on."
        )


def initial(execution_id: str, version: int = WORKFLOW_VERSION) -> ExecutionState:
    _assert_supported(version)
    return ExecutionState(execution_id=execution_id, version=version)


def retry_policy(state: ExecutionState) -> Optional[Mapping[str, Any]]:
    """The envelope's recorded retry policy, or ``None`` when it granted no retries."""
    policy = state.envelope.get("retry_policy")
    return policy if isinstance(policy, Mapping) else None


def retry_delay_seconds(state: ExecutionState) -> int:
    """The delay before the next wait round fires. Pure: a function of recorded facts.

    Exponential over paid attempts for a retry, the flat initial delay for a wait round,
    capped, plus jitter derived from the execution id and round rather than from a random
    source -- so every replay names the same delay, and the value that was used is
    recorded on the timer it created.
    """
    policy = retry_policy(state) or DEFAULT_RETRY_POLICY
    initial_seconds = int(policy.get("initial_seconds", 60))
    factor = max(1, int(policy.get("factor", 2)))
    cap = int(policy.get("cap_seconds", 600))
    exponent = max(0, state.attempt_no - 1) if state.failure_class in RETRYABLE_CLASSES else 0
    base = min(cap, initial_seconds * factor**exponent)
    spread = max(0, int(policy.get("jitter_seconds", 0)))
    if not spread:
        return base
    seed = hashlib.sha256(f"{state.execution_id}:{state.wait_round}".encode()).digest()
    return base + seed[0] % (spread + 1)


def reduce(state: ExecutionState, event: Event) -> ExecutionState:
    """Apply one event. Pure; raises ``HistoryIncompatible`` on anything unrecognised."""
    if event.kind not in EVENT_KINDS:
        raise HistoryIncompatible(
            f"event {event.sequence} of {state.execution_id} has kind {event.kind!r}, which "
            f"workflow version {state.version} does not define"
        )
    if event.sequence <= state.last_sequence:
        # A snapshot already covers it. Replay is by sequence, so this is not an error.
        return state
    payload = event.payload
    advanced = replace(state, last_sequence=event.sequence)

    if event.kind == ACCEPTED:
        version = int(payload.get("workflow_version", state.version))
        _assert_supported(version)
        return replace(
            advanced,
            version=version,
            state=S_ACCEPTED if state.state == S_NEW else state.state,
            envelope=dict(payload.get("envelope") or {}),
            owner_mode=str(payload.get("owner_mode") or state.owner_mode),
        )
    if state.terminal:
        # A terminal execution ignores everything after the fact except its own record of
        # it; nothing may reopen it by appending.
        return advanced
    if event.kind == ADMITTED:
        return replace(
            advanced,
            state=S_STOPPING if state.stop_generation else S_LAUNCHING,
            run_id=str(payload.get("run_id")),
            attempt_no=int(payload.get("attempt_no") or state.attempt_no + 1),
            session_id=None,
            observation=None,
            failure_class=None,
            wait_round=0,
            timer_id=None,
            timer_fired=False,
            policy=None,
            escalated=None,
            attempt_live=True,
        )
    if event.kind == LAUNCHED:
        if not state.attempt_live or payload.get("run_id") not in (None, state.run_id):
            return advanced
        return replace(
            advanced,
            state=S_STOPPING if state.stop_generation else S_WORKING,
            session_id=payload.get("session_id") or state.session_id,
        )
    if event.kind == OBSERVED:
        return replace(advanced, observation=str(payload.get("phase") or ""))
    if event.kind == SIGNAL:
        signal = str(payload.get("source_event_id") or "")
        if not signal or signal in state.pending_signals:
            return advanced
        return replace(advanced, pending_signals=state.pending_signals + (signal,))
    if event.kind == STOP_REQUESTED:
        return replace(
            advanced,
            state=S_STOPPING,
            stop_generation=max(state.stop_generation, int(payload.get("generation") or 1)),
            stop_requester=payload.get("requester") or state.stop_requester,
        )
    if event.kind == CONCLUDED:
        outcome = str(payload.get("outcome") or "")
        if payload.get("retry_owed") and outcome != "cancelled":
            return replace(
                advanced,
                state=S_RETRY_WAIT,
                failure_class=str(payload.get("failure_class") or WORKER_GONE),
                attempt_live=False,
                session_id=None,
                wait_round=0,
                timer_id=None,
                timer_fired=False,
                policy=None,
            )
        return replace(
            advanced,
            state=S_CANCELLED if outcome == "cancelled" else S_CONCLUDED,
            outcome=outcome,
            attempt_live=False,
        )
    if event.kind == CLOSED:
        outcome = str(payload.get("outcome") or "")
        return replace(
            advanced,
            state=S_CANCELLED if outcome == "cancelled" else S_CONCLUDED,
            outcome=outcome,
            failure_class=payload.get("failure_class") or state.failure_class,
            attempt_live=False,
        )
    if event.kind == MIGRATED:
        return replace(advanced, owner_mode=str(payload.get("to") or state.owner_mode))
    if event.kind == STAND_DOWN_REQUESTED:
        # A Stop already on record outranks a transfer: the intent is revoked, and a
        # stand-down arriving after it cannot un-revoke it.
        if state.stop_generation:
            return replace(advanced, transfer_to=str(payload.get("transfer_to") or ""))
        return replace(
            advanced,
            state=S_STANDING_DOWN,
            transfer_to=str(payload.get("transfer_to") or ""),
        )
    if event.kind == SIGNAL_DISPOSED:
        signal = str(payload.get("source_event_id") or "")
        return replace(
            advanced,
            pending_signals=tuple(item for item in state.pending_signals if item != signal),
        )
    if event.kind == TIMER_SET:
        return replace(
            advanced,
            timer_id=str(payload.get("timer_id") or ""),
            timer_fired=False,
            policy=None,
            wait_round=state.wait_round + 1,
        )
    if event.kind == TIMER_FIRED:
        if payload.get("timer_id") != state.timer_id:
            return advanced
        return replace(advanced, timer_fired=True)
    if event.kind == POLICY_OBSERVED:
        if payload.get("round") != state.wait_round:
            return advanced  # an observation for a round this execution has moved past
        return replace(advanced, policy=dict(payload))
    if event.kind == ACTIVITY_RESULT:
        return _apply_result(advanced, payload)
    return advanced  # pragma: no cover - EVENT_KINDS is exhaustive above


def _apply_result(state: ExecutionState, payload: Mapping[str, Any]) -> ExecutionState:
    kind = payload.get("kind")
    result_state = payload.get("state")
    error_class = payload.get("error_class")
    if kind == "launch_reconcile" and result_state == "unknown":
        return replace(state, state=S_PARKED, failure_class=str(error_class or EFFECT_UNKNOWN))
    if kind == "escalate" and result_state == "applied":
        return replace(state, escalated=str(error_class or state.failure_class or "escalated"))
    if kind == "relaunch" and result_state == "not_applied":
        cause = str(error_class or POLICY_WAIT)
        if cause in WAIT_CLASSES:
            # Another round of the same wait: a fresh timer, a fresh observation.
            return replace(state, timer_id=None, timer_fired=False, policy=None)
        return replace(state, failure_class=cause, timer_id=None, timer_fired=False)
    return state


def replay(
    execution_id: str,
    events: Iterable[Event],
    *,
    version: int = WORKFLOW_VERSION,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> ExecutionState:
    """Reduce a history, optionally starting from a snapshot. Identical inputs, identical state."""
    state = (
        ExecutionState.from_data(snapshot)
        if snapshot is not None
        else initial(execution_id, version)
    )
    for event in sorted(events, key=lambda item: item.sequence):
        state = reduce(state, event)
    return state


def _escalation(state: ExecutionState, cause: str) -> Tuple[Intent, ...]:
    if state.escalated:
        return ()
    attempt = state.run_id or "none"
    return (
        Intent(
            "escalate",
            f"{state.execution_id}:escalate:{attempt}:{cause}",
            {
                "failure_class": cause,
                "run_id": state.run_id,
                "attempt_no": state.attempt_no,
                "close": not state.attempt_live,
            },
        ),
    )


def next_intents(state: ExecutionState) -> Tuple[Intent, ...]:
    """What should happen next, as stable, idempotent proposals. Pure."""
    if state.terminal:
        return ()
    eid = state.execution_id
    if state.stop_generation and state.run_id and state.attempt_live:
        return (
            Intent(
                "stop",
                f"{eid}:stop:{state.run_id}:{state.stop_generation}",
                {"run_id": state.run_id, "generation": state.stop_generation},
            ),
        )
    if state.state == S_STANDING_DOWN and state.run_id:
        # Distinct from `stop` in its activity id and its kind, so an adapter can never
        # perform a transfer through the cancellation path or the reverse.
        return (
            Intent(
                "stand_down",
                f"{eid}:stand_down:{state.run_id}",
                {"run_id": state.run_id, "transfer_to": state.transfer_to or ""},
            ),
        )
    if state.state == S_ACCEPTED:
        return (
            Intent(
                "admit",
                f"{eid}:admit:{state.attempt_no + 1}",
                {"attempt_no": state.attempt_no + 1},
            ),
        )
    if state.state == S_LAUNCHING and state.run_id:
        if state.version >= 2:
            return (
                Intent(
                    "launch_reconcile",
                    f"{eid}:launch_reconcile:{state.run_id}",
                    {"run_id": state.run_id},
                ),
            )
        return (
            Intent(
                "launch",
                f"{eid}:launch:{state.run_id}",
                {"run_id": state.run_id, "envelope_keys": sorted(state.envelope)},
            ),
        )
    if state.state == S_WORKING and state.run_id:
        intents = [
            Intent("observe", f"{eid}:observe:{state.run_id}", {"run_id": state.run_id}),
        ]
        intents.extend(
            Intent(
                "deliver_signal",
                f"{eid}:signal:{signal}",
                {"run_id": state.run_id, "source_event_id": signal},
            )
            for signal in state.pending_signals
        )
        return tuple(intents)
    if state.state == S_PARKED:
        return _escalation(state, state.failure_class or EFFECT_UNKNOWN)
    if state.state == S_RETRY_WAIT:
        return _retry_intents(state)
    return ()


def _retry_intents(state: ExecutionState) -> Tuple[Intent, ...]:
    eid = state.execution_id
    cause = state.failure_class or WORKER_GONE
    policy = retry_policy(state)
    if cause not in RETRYABLE_CLASSES:
        return _escalation(state, cause)
    if policy is None:
        return _escalation(state, cause)
    if state.attempt_no >= int(policy.get("max_attempts", 1)):
        return _escalation(state, ATTEMPTS_EXHAUSTED)
    if state.wait_round >= int(policy.get("max_wait_rounds", 30)) and state.timer_id is None:
        return _escalation(state, state.policy.get("class") if state.policy else POLICY_WAIT)
    next_attempt = state.attempt_no + 1
    if state.timer_id is None:
        return (
            Intent(
                "schedule_retry",
                f"{eid}:schedule:{next_attempt}:{state.wait_round + 1}",
                {
                    "attempt_no": next_attempt,
                    "round": state.wait_round + 1,
                    "delay_seconds": retry_delay_seconds(state),
                    "timer_id": f"{eid}:retry:{next_attempt}:{state.wait_round + 1}",
                },
            ),
        )
    if not state.timer_fired:
        return ()
    if state.policy is None:
        return (
            Intent(
                "observe_policy",
                f"{eid}:policy:{next_attempt}:{state.wait_round}",
                {"attempt_no": next_attempt, "round": state.wait_round},
            ),
        )
    if state.policy.get("permitted"):
        return (
            Intent(
                "relaunch",
                f"{eid}:relaunch:{next_attempt}:{state.wait_round}",
                {
                    "attempt_no": next_attempt,
                    "round": state.wait_round,
                    "continues_execution_id": eid,
                },
            ),
        )
    blocked = str(state.policy.get("class") or POLICY_REVOKED)
    if blocked in WAIT_CLASSES:
        # The observation said "not now", not "never": wait another round. The timer id
        # names the round, so this is a new proposal rather than a repeat of the last.
        return (
            Intent(
                "schedule_retry",
                f"{eid}:schedule:{next_attempt}:{state.wait_round + 1}",
                {
                    "attempt_no": next_attempt,
                    "round": state.wait_round + 1,
                    "delay_seconds": retry_delay_seconds(state),
                    "timer_id": f"{eid}:retry:{next_attempt}:{state.wait_round + 1}",
                },
            ),
        )
    return _escalation(state, blocked)


__all__ = [
    "ACCEPTED",
    "ACTIVITY_RESULT",
    "ADMITTED",
    "ATTEMPTS_EXHAUSTED",
    "BUDGET_EXHAUSTED",
    "CAPACITY_WAIT",
    "CLOSED",
    "CONCLUDED",
    "COOLDOWN",
    "DEFAULT_RETRY_POLICY",
    "EFFECT_UNKNOWN",
    "EVENT_KINDS",
    "Event",
    "ExecutionState",
    "Intent",
    "LAUNCHED",
    "LAUNCH_NOT_APPLIED",
    "MIGRATED",
    "OBSERVED",
    "POLICY_OBSERVED",
    "POLICY_REVOKED",
    "POLICY_WAIT",
    "RETRYABLE_CLASSES",
    "SIGNAL",
    "SIGNAL_DISPOSED",
    "STAND_DOWN_REQUESTED",
    "STOP_REQUESTED",
    "SUPPORTED_VERSIONS",
    "TERMINAL_STATES",
    "TIMER_FIRED",
    "TIMER_SET",
    "WAIT_CLASSES",
    "WORKER_FAILED",
    "WORKER_GONE",
    "WORKFLOW_VERSION",
    "initial",
    "next_intents",
    "reduce",
    "replay",
    "retry_delay_seconds",
    "retry_policy",
]
