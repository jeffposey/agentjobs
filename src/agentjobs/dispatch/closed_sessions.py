"""End a dispatched session that lingers after its task has closed (task-566).

**Why this exists.** task-482 made the slot follow the work: a run whose task closed gives
its slot back and stays live. It also listed "the finish kills its own session" under *Not
to do*, because the run it was protecting was in a live exchange with the owner after the
merge. And the idle sweep protects every session with a run record. So a session that
cannot exit on its own -- task-557's could not, because its review sandboxes were live
background jobs -- lived for as long as nobody wrote to it.

The scripted finish now stops the worktree's processes (``dispatch/worktree_teardown.py``),
which is what normally lets such a session end its turn and exit by itself. This is the
backstop for one that still lingers, and it **narrows** task-482's rejection rather than
reversing it: a session is stopped only when

* its task is closed and its slot has been released for :data:`GRACE_SECONDS` -- time for
  the session to have exited by itself, which is the ordinary case;
* it is **idle** -- its transcript's newest turn event is the end of a turn, and nothing
  has been written for :data:`QUIET_SECONDS`;
* **no human message has reached it** since its review handoff (or, with no review
  handoff, since the prompt that dispatched it). That is task-482's case, and a session
  in it is never stopped: the skip is written onto the task, once.

Immediately before the stop the transcript is read again, and a session that has written
anything since is left for the next tick -- the same re-check the idle sweep makes. The
stop is ``claude stop`` through ``DispatchLedger._stop_session``, which keeps the
conversation resumable and ends what the session leaves behind by creation time (task-548);
the run is then concluded ``completed`` with a ``dispatch_result`` saying why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.ledger import RunRecord, live_runs, write_status
from agentjobs.dispatch.slots import TASK_CLOSED

GRACE_SECONDS = 30.0
"""How long after the close a session is given to exit by itself before this acts.

The finish closes the task, then stops the worktree's processes and removes it, and only
then does the session hear that its background jobs ended and take the turn that lets it
exit. Measured on 2026-09-24, a session with nothing left running exits 5 to 15 seconds
after its last turn; thirty leaves that path the first chance every time, and together
with the poll interval keeps the backstop inside the minute ac-2 asks for."""

QUIET_SECONDS = 10.0
"""How long the transcript must have gone unwritten before a turn end is believed."""

NON_HUMAN_ORIGINS = frozenset({"task-notification"})
"""Prompt origins that are the harness talking, not a person.

**Default-deny**: a prompt whose origin is anything else, or absent, counts as a person.
The cost of that mistake is a session left running, which is today's behaviour; the cost
of the opposite mistake is stopping a conversation somebody is having."""

SKIPPED_KEY = "closed_session_stop_skipped"
"""Written into the run's meta when the stop is refused for a human message, so it is
refused -- and written onto the task -- once, not every ten seconds."""


@dataclass(frozen=True)
class TranscriptReading:
    """What a session's transcript says about whether it may be stopped."""

    mid_turn: bool
    human_at: Optional[str]
    """When a person last wrote to it after the reference moment, or None."""
    mtime: float


def _moment(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_prompt(event: Mapping[str, Any]) -> bool:
    """A user event that is a prompt, rather than a tool result or a harness aside."""
    if event.get("type") != "user" or event.get("isMeta"):
        return False
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result" for block in content
        )
    return False


def _origin(event: Mapping[str, Any]) -> str:
    origin = event.get("origin")
    if isinstance(origin, dict):
        return str(origin.get("kind") or "")
    return ""


def read_transcript(
    path: Path, *, run_started: Optional[datetime], review_handoff: Optional[datetime]
) -> TranscriptReading:
    """Judge a session's transcript. Raises ``OSError`` when it cannot be read.

    **Mid-turn** unless the newest turn-bearing event is a ``turn_duration`` -- the line
    Claude Code writes when a turn ends. A prompt, an assistant message, a tool result or a
    queued message after it means a turn is open or about to be. No turn events at all is
    mid-turn too: nothing has been proved.

    **The reference moment** for human messages is the review handoff, or with none the
    first prompt at or after the run's start -- the dispatch's own prompt, which has a
    human origin because it arrives the way a typed message does, and is excluded by being
    the reference itself.
    """
    mtime = path.stat().st_mtime
    open_turn = True
    seen_any = False
    first_prompt: Optional[datetime] = None
    human: List[datetime] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "turn_duration":
                open_turn, seen_any = False, True
            elif kind in ("user", "assistant"):
                open_turn, seen_any = True, True
            elif kind == "queue-operation" and event.get("operation") == "enqueue":
                open_turn, seen_any = True, True
            if _is_prompt(event) and _origin(event) not in NON_HUMAN_ORIGINS:
                at = _moment(event.get("timestamp"))
                if at is None:
                    continue
                if run_started is not None and at < run_started:
                    continue
                if first_prompt is None:
                    first_prompt = at
                human.append(at)
    reference = review_handoff or first_prompt
    later = [at for at in human if reference is not None and at > reference]
    if reference is None and human:
        later = human
    return TranscriptReading(
        mid_turn=open_turn or not seen_any,
        human_at=max(later).isoformat() if later else None,
        mtime=mtime,
    )


def review_handoff_at(task: Any, since: Optional[datetime]) -> Optional[datetime]:
    """The newest handoff to ``human``/``review`` on ``task`` at or after ``since``."""
    found: Optional[datetime] = None
    for entry in getattr(task, "log", []) or []:
        if str(getattr(entry, "type", "")).rsplit(".", 1)[-1].lower() != "handoff":
            continue
        data = getattr(entry, "data", None) or {}
        ball = getattr(data.get("ball"), "value", data.get("ball"))
        reason = getattr(data.get("ball_reason"), "value", data.get("ball_reason"))
        if str(ball) != "human" or str(reason) != "review":
            continue
        at = getattr(entry, "ts", None)
        if isinstance(at, str):
            at = _moment(at)
        if not isinstance(at, datetime):
            continue
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        if since is not None and at < since:
            continue
        if found is None or at > found:
            found = at
    return found


@dataclass
class Deps:
    """The machine and the store, injectable. The poller supplies the real ones."""

    task: Callable[[RunRecord], Any]
    """The run's task, or None when it cannot be read."""
    transcript: Callable[[RunRecord], Optional[Path]]
    stop: Callable[[RunRecord], Tuple[bool, str]]
    """``claude stop`` and the reap of what it leaves: ``DispatchLedger._stop_session``."""
    conclude: Callable[[RunRecord, str], None]
    """Write the run's ``completed`` result, with this body."""
    note: Callable[[RunRecord, str, Dict[str, Any]], None]
    """Append a note to the run's task."""
    meta: Callable[[RunRecord], Dict[str, Any]]
    now: Callable[[], datetime] = field(default_factory=lambda: dispatch_clock.utcnow)
    wall: Callable[[], float] = field(default_factory=lambda: _wall)
    runs: Callable[[], List[RunRecord]] = field(default_factory=lambda: _no_runs)


def _wall() -> float:
    return dispatch_clock.utcnow().timestamp()


def _no_runs() -> List[RunRecord]:
    return []


def end_lingering(record: RunRecord, deps: Deps) -> Optional[str]:
    """Stop one lingering session if every condition holds. Returns what happened, or None.

    None is "not this tick": not closed long enough, mid-turn, or recently written. Every
    other answer is a sentence for the poller's report.
    """
    if not record.is_session or not record.is_live or not record.session_id:
        return None
    if not record.slot_released or record.slot_released_reason != TASK_CLOSED:
        return None
    released = record.slot_released_at
    if released is None or (deps.now() - released).total_seconds() < GRACE_SECONDS:
        return None
    if deps.meta(record).get(SKIPPED_KEY):
        return None
    task = deps.task(record)
    if task is None or getattr(task, "is_open", True):
        return None
    path = deps.transcript(record)
    if path is None:
        return None
    handoff = review_handoff_at(task, record.started_at)
    try:
        reading = read_transcript(path, run_started=record.started_at, review_handoff=handoff)
    except OSError:
        return None

    if reading.human_at is not None:
        since = "its review handoff" if handoff is not None else "the prompt that dispatched it"
        body = (
            f"Not stopping run {record.run_id}'s session although {record.task_id} is closed: "
            f"a person wrote to it at {reading.human_at}, after {since}, so it may be in an "
            "exchange that outlives the merge (task-482's case). It is left running and will "
            "not be stopped automatically; it ends when it exits or is stopped by hand "
            "(task-566)."
        )
        write_status(record, **{SKIPPED_KEY: {"human_at": reading.human_at}})
        deps.note(record, body, {"run_id": record.run_id, "stop_skipped": "human_message"})
        return f"left running: a person wrote to it at {reading.human_at}"

    if reading.mid_turn or deps.wall() - reading.mtime < QUIET_SECONDS:
        return None

    # The re-check, immediately before the stop: anything written since the read above
    # means the session is doing something, and the next tick can judge it again.
    try:
        again = read_transcript(path, run_started=record.started_at, review_handoff=handoff)
    except OSError:
        return None
    if again.mtime != reading.mtime or again.mid_turn or again.human_at is not None:
        return None

    ok, detail = deps.stop(record)
    if not ok:
        return f"could not stop its lingering session: {detail}"
    deps.conclude(
        record,
        (
            f"{record.task_id} closed and this session was still open, idle, with no message "
            f"from a person since its review, {int(GRACE_SECONDS)}s after the close; so it was "
            f"stopped (task-566): {detail}. The conversation is kept: "
            f"`claude --resume {record.session_id}` or attach it from the app."
        ),
    )
    return f"stopped its lingering session: {detail}"


def sweep(deps: Deps) -> List[Tuple[str, str]]:
    """``(run_id, what happened)`` for every live run this tick acted on. Never raises."""
    results: List[Tuple[str, str]] = []
    try:
        records = deps.runs()
    except OSError:
        return results
    for record in records:
        try:
            outcome = end_lingering(record, deps)
        except Exception as exc:  # noqa: BLE001 - one run must not stop the sweep
            outcome = f"closed-session check failed: {exc}"
        if outcome:
            results.append((record.run_id, outcome))
    return results


def _find_transcript(session_id: str, cwd: Path) -> Optional[Path]:
    from agentjobs.dispatch.transcript import find_session_transcript

    return find_session_transcript(session_id, cwd)


TRANSCRIPT_FINDER: Callable[[str, Path], Optional[Path]] = _find_transcript
"""Where the poller finds a session's transcript. The suite installs one that finds none,
so a test's fake run can never be matched to a real session and stopped (``conftest``)."""


def live_session_runs(home: Path) -> Callable[[], List[RunRecord]]:
    return lambda: [record for record in live_runs(home) if record.is_session]


__all__ = [
    "Deps",
    "GRACE_SECONDS",
    "QUIET_SECONDS",
    "SKIPPED_KEY",
    "TranscriptReading",
    "end_lingering",
    "live_session_runs",
    "read_transcript",
    "review_handoff_at",
    "sweep",
]
