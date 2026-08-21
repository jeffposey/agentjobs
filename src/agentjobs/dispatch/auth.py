"""Recognising a dispatched session that died on an expired login.

Claude Code does not refresh its own credential per session. A shared background daemon
owns the OAuth refresh for every ``--bg`` worker, and on 2026-08-21 that refresh failed
four times over three minutes, after which the daemon discarded a token its own log line
calls ``(token still valid)``. Every dispatched session on the machine died mid-turn.
task-224 has the timeline and the evidence.

**The reason this needs a module rather than a phase check is that the failure is
invisible to everything dispatch already looks at.** A permission park leaves the session
alive with a pending prompt, which the ledger reports as ``waiting``/``blocked`` and
``poll_session`` turns into a handoff. An auth failure does the opposite: it *ends the
turn*. The session emits one synthetic assistant message and goes idle with nothing
pending, so ``claude agents --json`` reports ``idle``/``done`` -- indistinguishable from
a session that finished its work. run_a1e35ca5 is in the ledger as ``outcome:
completed`` after losing six minutes to a dead credential and needing a human to notice.

So the signal has to come from somewhere else, and there is exactly one place it lives:
the session's own JSONL transcript under the Claude home, where the failing turn is a
single line carrying ``"error": "authentication_failed"``. Across every session log on
this machine that field had three occurrences and all three were genuine.

Two properties of the failure shape the detector:

- **It is not a substring search.** The string ``authentication_failed`` appears in the
  transcript of any session that has *read about* this bug -- this file, the task record,
  these tests. Every line is parsed as JSON and matched on the top-level ``error`` field,
  and the line's own session id is checked against the run's.
- **It clears.** Re-authenticating and nudging the session resumes it in place, and the
  dead line stays in the file forever. A failure followed by a real assistant message is
  therefore history, not a stall, and this module reports nothing for it.

Nothing here reads ``~/.claude/daemon.log`` or ``daemon-auth-status.json``. Both were
considered and rejected in task-224: the first is an undocumented internal, and the
second is a latch that is never cleared -- it still read ``auth_required`` three hours
after a successful re-auth, while six workers ran happily on the new token.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

CLAUDE_HOME_ENV = "CLAUDE_CONFIG_DIR"
"""Claude Code's own override for where its home lives. Honoured rather than invented."""

AUTH_ERROR = "authentication_failed"
"""The exact value of the transcript line's top-level ``error`` field."""

TAIL_BYTES = 262_144
"""How much of a session transcript is read per poll.

A transcript runs to megabytes and is read every ten seconds per live run, so reading all
of it would make the poller the most expensive thing in the process. The window is also
the right *semantics* and not only the cheap one: this module answers "is this session
stalled **now**", and a failure that has scrolled out of a quarter-megabyte of newer
lines is a session that carried on working afterwards.
"""

SYNTHETIC_MODEL = "<synthetic>"
"""The model on a locally-generated error message. A real reply names a real model."""

_UUID_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class AuthStall:
    """A session whose most recent word was "login expired", with nothing after it."""

    session_id: str
    """The id as the run ledger knows it -- short, as ``claude agents --json`` prints."""

    at: datetime
    """When the failing turn was written, in UTC. Used as the idempotency key."""

    message: str
    """What the session said, verbatim -- normally ``Login expired · Please run /login``."""

    log_path: Path
    """The transcript it was read from, so a human can go and look."""


def claude_home(override: Optional[Path] = None) -> Path:
    """Where Claude Code keeps its sessions: an explicit path, the env var, or ``~/.claude``."""
    if override is not None:
        return Path(override)
    configured = os.environ.get(CLAUDE_HOME_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".claude"


def session_log_path(session_id: str, *, home: Optional[Path] = None) -> Optional[Path]:
    """The JSONL transcript for a session id, or ``None`` when there is no such thing.

    Two routes, exact first. A ``--bg`` worker keeps a state file at
    ``jobs/<short-id>/state.json`` naming its own transcript, which is unambiguous and
    costs one small read. Failing that, the transcript is named for the full session UUID
    under ``projects/<munged-cwd>/``, and the ledger's short id is that UUID's prefix.

    Returning ``None`` is the ordinary answer for a runner that is not Claude Code. No
    other runner writes any of this, so an auth check against one finds nothing and the
    poll carries on -- which is why nothing in this package sniffs at runner names.
    """
    root = claude_home(home)
    named = _path_from_job_state(root, session_id)
    if named is not None:
        return named
    matches = [
        candidate
        for candidate in root.glob(f"projects/*/{session_id}*.jsonl")
        if _UUID_NAME.match(candidate.stem)
    ]
    if not matches:
        return None
    return max(matches, key=lambda candidate: candidate.stat().st_mtime)


def _path_from_job_state(root: Path, session_id: str) -> Optional[Path]:
    """The transcript a ``--bg`` job names for itself, when it named one."""
    state = root / "jobs" / session_id / "state.json"
    try:
        loaded = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    named = loaded.get("linkScanPath")
    if isinstance(named, str) and named:
        candidate = Path(named)
        if candidate.is_file():
            return candidate
    return None


def read_auth_stall(
    session_id: str,
    *,
    home: Optional[Path] = None,
    since: Optional[datetime] = None,
    tail_bytes: int = TAIL_BYTES,
) -> Optional[AuthStall]:
    """Report a session stalled on an expired login, or ``None`` for every other state.

    ``since`` is the run's start. A session id can be resumed, and a transcript outlives
    the run that wrote it, so a failure recorded before this run began says nothing about
    this run.
    """
    path = session_log_path(session_id, home=home)
    if path is None:
        return None
    try:
        lines = _tail_lines(path, tail_bytes)
    except OSError:  # pragma: no cover - the transcript went away mid-read
        return None
    stall = _stall_in(lines, session_id=session_id, path=path)
    if stall is None:
        return None
    if since is not None and stall.at < since:
        return None
    return stall


def _tail_lines(path: Path, tail_bytes: int) -> List[str]:
    """The last whole lines of a file, without reading the rest of it.

    The first line of the window is dropped when the window did not start at the top of
    the file, because a byte offset lands mid-record and half a JSON object is not one.
    """
    size = path.stat().st_size
    start = max(0, size - tail_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def _stall_in(lines: Iterable[str], *, session_id: str, path: Path) -> Optional[AuthStall]:
    """Walk a transcript window forwards; the last thing that happened wins.

    Forwards rather than backwards on purpose. Reading from the end would find the
    failure and stop, and the whole question is whether anything came *after* it: a
    session that was re-authenticated and nudged has a real reply below the dead line and
    is not stalled. A human's message does not clear it -- verified on 2026-08-21, when a
    message sent to a stalled session failed nine milliseconds after arriving, because it
    was retried against the credential the daemon had already discarded.
    """
    stall: Optional[AuthStall] = None
    for entry in _entries(lines):
        if _is_auth_failure(entry, session_id):
            moment = _moment(entry.get("timestamp"))
            if moment is None:
                continue
            stall = AuthStall(
                session_id=session_id,
                at=moment,
                message=_text_of(entry),
                log_path=path,
            )
        elif _is_real_reply(entry):
            stall = None
    return stall


def _entries(lines: Iterable[str]) -> Iterator[dict]:
    """The JSON objects in a transcript window. Anything else is skipped in silence."""
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            loaded = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(loaded, dict):
            yield loaded


def _is_auth_failure(entry: dict, session_id: str) -> bool:
    """The failing turn: an API error line whose error is an auth one, from this session.

    The session id check is what makes this safe to run against a transcript located by a
    prefix glob. It also costs nothing.
    """
    if entry.get("error") != AUTH_ERROR:
        return False
    if entry.get("isApiErrorMessage") is not True:
        return False
    return _belongs_to(entry, session_id)


def _belongs_to(entry: dict, session_id: str) -> bool:
    """True when a transcript line names this session, or names none at all."""
    named = entry.get("sessionId") or entry.get("session_id")
    if not isinstance(named, str) or not named:
        return True
    return named.startswith(session_id)


def _is_real_reply(entry: dict) -> bool:
    """An assistant turn the model actually produced, which proves auth is working again."""
    if entry.get("type") != "assistant":
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("model") not in {None, SYNTHETIC_MODEL}


def _text_of(entry: dict) -> str:
    """The human-readable text of an error line, for quoting into a ball prompt."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def _moment(raw: object) -> Optional[datetime]:
    """A transcript timestamp as an aware UTC datetime, or ``None`` if it is not one."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
