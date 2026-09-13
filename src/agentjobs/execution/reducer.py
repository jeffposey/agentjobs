"""The versioned, pure reducer: where an execution stands, computed only from its history.

Design section 9a asks for "a small explicit state machine", and this is it. Two rules
make it replayable, and a test holds each one:

* **It reads nothing but events.** No clock, no configuration, no roster, no filesystem.
  A timer firing, a poll result, a Stop -- anything from the world arrives as an event an
  adapter appended, and a fresh process replaying the same events reaches the same state
  and proposes the same intents. That is what lets the server after a restart, a CLI
  invocation or a new poller resume an execution none of them started.
* **It is versioned, and an unknown version is refused, never guessed.** Replaying old
  events through whichever control flow happens to be installed is the failure DBOS's
  upgrade guidance names; ``HistoryIncompatible`` is raised instead, and the store refuses
  the mutation that would have followed.

Intents are proposals with **stable activity ids**. Replaying a history twice names the
same activities, so recording them is idempotent and a replay by itself performs nothing.
Carrying them out is an adapter's job, outside any transaction (``coordinator``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from agentjobs.execution.errors import HistoryIncompatible

WORKFLOW_VERSION = 1
"""The workflow definition this reducer implements. Independent of the package version:
a release that changes no transition must not strand every history written before it."""

SUPPORTED_VERSIONS = frozenset({WORKFLOW_VERSION})

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

EVENT_KINDS = frozenset(
    {ACCEPTED, ADMITTED, LAUNCHED, OBSERVED, SIGNAL, STOP_REQUESTED, CONCLUDED, MIGRATED}
)

# Execution states (design section 9a's diagram). Not task lifecycle values.
S_NEW = "new"
S_ACCEPTED = "accepted"
S_LAUNCHING = "launching"
S_WORKING = "working"
S_STOPPING = "stopping"
S_CANCELLED = "cancelled"
S_CONCLUDED = "concluded"

TERMINAL_STATES = frozenset({S_CANCELLED, S_CONCLUDED})


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
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> "ExecutionState":
        version = int(data.get("version", 0))
        _assert_supported(version)
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
        )
    if event.kind == LAUNCHED:
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
        return replace(
            advanced,
            state=S_CANCELLED if outcome == "cancelled" else S_CONCLUDED,
            outcome=outcome,
        )
    if event.kind == MIGRATED:
        return replace(advanced, owner_mode=str(payload.get("to") or state.owner_mode))
    return advanced  # pragma: no cover - EVENT_KINDS is exhaustive above


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


def next_intents(state: ExecutionState) -> Tuple[Intent, ...]:
    """What should happen next, as stable, idempotent proposals. Pure."""
    if state.terminal:
        return ()
    eid = state.execution_id
    if state.stop_generation and state.run_id:
        return (
            Intent(
                "stop",
                f"{eid}:stop:{state.run_id}:{state.stop_generation}",
                {"run_id": state.run_id, "generation": state.stop_generation},
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
    return ()


__all__ = [
    "ACCEPTED",
    "ADMITTED",
    "CONCLUDED",
    "EVENT_KINDS",
    "Event",
    "ExecutionState",
    "Intent",
    "LAUNCHED",
    "MIGRATED",
    "OBSERVED",
    "SIGNAL",
    "STOP_REQUESTED",
    "SUPPORTED_VERSIONS",
    "TERMINAL_STATES",
    "WORKFLOW_VERSION",
    "initial",
    "next_intents",
    "reduce",
    "replay",
]
