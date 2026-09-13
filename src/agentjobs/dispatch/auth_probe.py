"""Asking a credential store whether it can reach the model again (task-417).

A parked session cannot say whether its credential has recovered: the dead turn stays in
its transcript forever, and a message sent to it is retried against whatever the store
holds. So the question goes to a **fresh process** that uses the same executable, model
and Claude home. It sends a tiny exact-response request with every tool and MCP server
switched off.

**Success is positive, never the absence of a failure.** task-224 entry 47 is the
regression this module exists to prevent. A probe harness counted six "You've hit your
monthly spend limit" refusals as healthy answers because none of them said
``Login expired``. Here a probe is ``ready`` only when all of these hold: the process
exits 0, prints the ``--output-format json`` result object, that object says
``subtype: success`` and ``is_error: false``, and its ``result`` is exactly the
requested token. Everything else falls into a named not-ready class, so a billing
refusal, a rate limit, a timeout and unparseable output stay distinguishable in the
record.

The argv was verified against Claude Code 2.1.270 on 2026-09-13. The command
``claude -p --model claude-opus-5 --output-format json --tools "" --strict-mcp-config
--no-session-persistence`` with the prompt on stdin answered in 2.7s for about $0.07.
``--bare`` is deliberately absent: it restricts authentication to an API key, so a probe
using it would not test the OAuth store at all.

This is a real model call and costs real money. That is why the recovery loop caps probes
per profile per hour and never runs two for one profile at once. A probe may trigger the
CLI's own token refresh, which is normal CLI behaviour. It is a confound for experiments
about *why* logins are lost, not something to suppress.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

PROBE_TOKEN = "AUTH_OK"
PROBE_PROMPT = f"Reply with exactly: {PROBE_TOKEN}"
PROBE_TIMEOUT_SECONDS = 30.0

PROBE_FLAGS = (
    "--output-format",
    "json",
    "--tools",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
)
"""No tools, no MCP servers, nothing saved. The model and executable are the waiter's own."""


class ProbeClass(str, Enum):
    """What one probe established. Only ``READY`` permits a nudge."""

    READY = "ready"
    AUTH_REJECTED = "auth_rejected"
    SPEND_LIMIT = "spend_limit"
    """A spend or billing limit only a person can raise. Never readiness (task-224 entry 47)."""
    USAGE_EXHAUSTED = "usage_exhausted"
    """A usage window (five-hour, weekly) that clears by itself at a reported time."""
    RATE_LIMITED = "rate_limited"
    """A transient 429 or overload with no reported reset."""
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True)
class ProbeRequest:
    """Everything a probe needs, none of it secret."""

    executable: Sequence[str]
    model: Optional[str]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float = PROBE_TIMEOUT_SECONDS

    def argv(self) -> List[str]:
        argv = [*self.executable, "-p"]
        if self.model:
            argv += ["--model", self.model]
        argv += list(PROBE_FLAGS)
        return argv


@dataclass(frozen=True)
class ProbeResult:
    klass: ProbeClass
    detail: str
    exit_code: Optional[int] = None
    resets_at: Optional[datetime] = None

    @property
    def ready(self) -> bool:
        return self.klass is ProbeClass.READY

    def as_record(self) -> Dict[str, object]:
        return {
            "class": self.klass.value,
            "detail": self.detail[:500],
            "exit_code": self.exit_code,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
        }


ProbeRunner = Callable[[ProbeRequest], ProbeResult]

_AUTH_PHRASES = (
    "login expired",
    "please run /login",
    "authentication_failed",
    "failed to authenticate",
    "invalid api key",
    "oauth",
    "not logged in",
    "401",
)
_SPEND_PHRASES = ("spend limit", "usage credits", "billing", "credit balance", "account_on_hold")
_USAGE_PHRASES = ("session limit", "usage limit", "weekly limit", "5-hour limit", "hit your limit")
_RATE_PHRASES = ("rate limit", "rate_limit", "overloaded", "429", "529", "too many requests")


def classify_probe(
    *,
    exit_code: Optional[int],
    stdout: str,
    stderr: str = "",
    timed_out: bool = False,
    now: Optional[datetime] = None,
) -> ProbeResult:
    """Judge one probe's output. Pure: every probe, real or fake, is judged here.

    The order matters. Readiness is decided first and only from the parsed result object.
    Every other class is decided from what the failure *says*, and spend is checked before
    the usage window. A spend-limit refusal has been seen carrying a five-hour reset time,
    and waiting for that reset would wait for something that does not clear it.
    """
    if timed_out:
        return ProbeResult(ProbeClass.TIMEOUT, "the probe did not answer in time", exit_code)
    payload = _result_object(stdout)
    text = _failure_text(payload, stdout, stderr)
    if (
        exit_code == 0
        and payload is not None
        and payload.get("type") == "result"
        and payload.get("subtype") == "success"
        and payload.get("is_error") is False
        and str(payload.get("result") or "").strip() == PROBE_TOKEN
    ):
        return ProbeResult(ProbeClass.READY, "the model answered the probe exactly", exit_code)
    lowered = text.lower()
    if any(phrase in lowered for phrase in _SPEND_PHRASES):
        return ProbeResult(ProbeClass.SPEND_LIMIT, text, exit_code)
    if any(phrase in lowered for phrase in _USAGE_PHRASES):
        return ProbeResult(
            ProbeClass.USAGE_EXHAUSTED, text, exit_code, resets_at=_reset_from(payload)
        )
    if any(phrase in lowered for phrase in _AUTH_PHRASES):
        return ProbeResult(ProbeClass.AUTH_REJECTED, text, exit_code)
    if any(phrase in lowered for phrase in _RATE_PHRASES):
        return ProbeResult(ProbeClass.RATE_LIMITED, text, exit_code)
    if exit_code == 0 and payload is not None and payload.get("is_error") is False:
        return ProbeResult(
            ProbeClass.MALFORMED,
            f"the model answered, but not with {PROBE_TOKEN}: {text[:200]}",
            exit_code,
        )
    return ProbeResult(ProbeClass.MALFORMED, text or "no recognisable output", exit_code)


def run_probe(request: ProbeRequest) -> ProbeResult:
    """Run one probe as a subprocess and classify it. Never raises."""
    try:
        request.cwd.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            request.argv(),
            input=PROBE_PROMPT,
            cwd=str(request.cwd),
            env=dict(request.env) or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=request.timeout,
        )
    except subprocess.TimeoutExpired:
        return classify_probe(exit_code=None, stdout="", timed_out=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult(ProbeClass.LAUNCH_FAILED, f"could not start the probe: {exc}")
    return classify_probe(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _result_object(stdout: str) -> Optional[dict]:
    """The last JSON object printed, which is where ``--output-format json`` puts it."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            loaded = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(loaded, dict):
            return loaded
    try:
        loaded = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _failure_text(payload: Optional[dict], stdout: str, stderr: str) -> str:
    parts: List[str] = []
    if payload is not None:
        for key in ("result", "error", "subtype", "api_error_status"):
            value = payload.get(key)
            if value not in (None, "", False):
                parts.append(str(value))
    else:
        parts.append((stdout or "").strip())
    parts.append((stderr or "").strip())
    return " ".join(part for part in parts if part).strip()


def _reset_from(payload: Optional[dict]) -> Optional[datetime]:
    """A structured reset time on the result object, when the CLI printed one."""
    if payload is None:
        return None
    for holder in (payload, payload.get("quotaLimits"), payload.get("rate_limit_info")):
        if not isinstance(holder, dict):
            continue
        raw = holder.get("resetsAt", holder.get("resets_at"))
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                return datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    return None
