"""Recovering a parked session without a person: probe, then wake, then check it (task-417).

``dispatch.auth`` made a dead login visible in the tracker (task-224) and stopped there.
The remaining human work was always the same three steps. First, find out whether the
credential store answers again. Second, if it does, wake the session that died on it.
Third, look for evidence that the session is working. This module does all three, and
it wakes a person only when a person is the only fix. Design section 9a, *Auth recovery*,
is the contract. This docstring covers where the implementation had to decide something
the design left open.

**An incident per (kind, credential profile), not per run.** Nine processes lose one
login together (task-417 entry 5), so nine runs share one probe loop and one
notification. A profile is the facts that decide whether one probe speaks for another run:
driver, executable, model, Claude home, and the *names* of the auth environment
variables. A desktop session on host-injected auth and an npm CLI session on
``.credentials.json`` are different profiles and never share readiness. Values are never
read.

Three kinds, because a quota refusal ends a turn exactly as a dead login does:

====================  ==================================  ==================================
kind                  first word to the tracker            probing
====================  ==================================  ==================================
``auth``              none for five minutes, then one      at once, then every 60s
                      human/input naming the login
``usage_limit``       external/service at once, naming     from the reported reset + 60s
                      the reset time
``spend_limit``       human/input at once                  every 15 min
====================  ==================================  ==================================

**Only a positive probe permits a nudge** (``dispatch.auth_probe``). A nudge is the
verified wake from task-417 entry 8: ``stop``, confirm the session has no pid, then
``--bg --resume <full uuid>`` with the message on stdin and no other flag, so the session
comes back with its own saved runner, model and posture. The intent is written before the
effect and the result after. While the intent stands, ``poll_session`` neither settles
nor concludes the run. A person's Stop still wins.

**A nudge's acknowledgement is not its outcome.** The launcher saying it woke the session
means the message left. The session working again is a separate fact: a real model reply
after the nudge, read from the transcript. A nudge whose result was never recorded is
reconciled from that same transcript, never resent blind. If the transcript shows neither
a delivered message nor a reply, the result is ``uncertain`` and gets one escalation.

**A nudge continues the attempt; it does not buy a new one.** The failed turn reached no
model, so admitting a fresh paid attempt per nudge would spend the per-task allowance on
turns that never ran. The bound is instead ``MAX_NUDGES_PER_RUN``, with policy
re-observed before every nudge (the kill switch, project enablement, hold, Stop,
stand-down).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from agentjobs.dispatch import auth, peers
from agentjobs.dispatch.auth_probe import (
    ProbeClass,
    ProbeRequest,
    ProbeResult,
    ProbeRunner,
    run_probe,
)
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.execution.store import ExecutionStore, digest, this_holder
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType

if TYPE_CHECKING:  # pragma: no cover
    from agentjobs.dispatch.runner import DispatchRunner, RunHandle
    from agentjobs.projects import ProjectRegistry
    from agentjobs.store_factory import TaskManagerLike

KIND_AUTH = "auth"
KIND_USAGE_LIMIT = auth.KIND_USAGE_LIMIT
KIND_SPEND_LIMIT = auth.KIND_SPEND_LIMIT

ACTOR = "dispatcher"
MARKER = "auth_recovery"
"""The ``data`` key on every entry this module writes, so a reader -- the epic walk, the
handoff-clearing check -- can tell its entries from a person's."""

PROBE_HOURLY_CAP = 60
MAX_NUDGES_PER_RUN = 3
NUDGE_LEASE_SECONDS = 300.0
"""How long a nudge intent may stand with no result before it is reconciled as abandoned."""
CONFIRM_SECONDS = 900.0
"""How long a nudged session has to produce a real reply before a person is told."""
RESET_GRACE_SECONDS = 60.0
UNKNOWN_RESET_PROBE_SECONDS = 1800.0

AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
)
"""Environment variables whose *presence* changes which credential a process uses."""

# Waiter statuses.
WAITING = "waiting"
NUDGING = "nudging"
NUDGED = "nudged"
RECOVERED = "recovered"
REMOVED = "removed"
UNCERTAIN = "uncertain"
ESCALATED = "escalated"
ACTIVE_STATUSES = (WAITING, NUDGING, NUDGED, UNCERTAIN)
HOLDING_STATUSES = frozenset({NUDGING, UNCERTAIN})
"""A run in one of these must not be settled by a poll: its session was deliberately
stopped for a resume, or whether it was is unknown."""


@dataclass(frozen=True)
class KindPolicy:
    probe_period: float
    notify_after: Optional[float]
    """Seconds from opening to the one human notification; ``None`` means never."""
    park: Optional[Tuple[Ball, BallReason]]
    """A handoff written as soon as a run joins, or ``None`` to say nothing yet."""


POLICIES: Dict[str, KindPolicy] = {
    KIND_AUTH: KindPolicy(probe_period=60.0, notify_after=300.0, park=None),
    KIND_USAGE_LIMIT: KindPolicy(
        probe_period=60.0, notify_after=None, park=(Ball.EXTERNAL, BallReason.SERVICE)
    ),
    KIND_SPEND_LIMIT: KindPolicy(
        probe_period=900.0, notify_after=0.0, park=(Ball.HUMAN, BallReason.INPUT)
    ),
}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----- what a stall is, and whose -------------------------------------------------


@dataclass(frozen=True)
class Stall:
    """A session's last word was a refusal this module can recover from."""

    kind: str
    at: datetime
    message: str
    resets_at: Optional[datetime] = None


@dataclass(frozen=True)
class Profile:
    """The credential context a probe must share to speak for a run. No values."""

    driver: str
    executable: Tuple[str, ...]
    model: Optional[str]
    claude_home: str
    auth_env: Tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return digest(self.as_json())

    def as_json(self) -> Dict[str, Any]:
        return {
            "driver": self.driver,
            "executable": list(self.executable),
            "model": self.model,
            "claude_home": self.claude_home,
            "auth_env": list(self.auth_env),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Profile":
        return cls(
            driver=str(data.get("driver") or ""),
            executable=tuple(str(item) for item in data.get("executable") or ()),
            model=data.get("model") if isinstance(data.get("model"), str) else None,
            claude_home=str(data.get("claude_home") or ""),
            auth_env=tuple(str(item) for item in data.get("auth_env") or ()),
        )


def model_from_argv(argv: object) -> Optional[str]:
    """The ``--model`` a run was launched with, from its recorded argv."""
    if not isinstance(argv, list):
        return None
    for index, element in enumerate(argv):
        if element == "--model" and index + 1 < len(argv) and isinstance(argv[index + 1], str):
            return str(argv[index + 1])
        if isinstance(element, str) and element.startswith("--model="):
            return element.split("=", 1)[1]
    return None


def profile_for(runner: "DispatchRunner", handle: "RunHandle") -> Profile:
    environment = runner._environment()
    return Profile(
        driver=runner.runner.driver.value,
        executable=tuple(runner.executable_prefix()),
        model=model_from_argv(handle.directory.read_meta().get("argv")),
        claude_home=str(auth.claude_home(runner.claude_home)),
        auth_env=tuple(name for name in AUTH_ENV_NAMES if environment.get(name)),
    )


def read_stall(
    session_id: str, *, home: Optional[Path], since: Optional[datetime]
) -> Optional[Stall]:
    """The newer of a session's auth or quota refusal, or ``None``."""
    found: List[Stall] = []
    dead = auth.read_auth_stall(session_id, home=home, since=since)
    if dead is not None:
        found.append(Stall(KIND_AUTH, dead.at, dead.message))
    limited = auth.read_limit_stall(session_id, home=home, since=since)
    if limited is not None:
        found.append(Stall(limited.kind, limited.at, limited.message, limited.resets_at))
    if not found:
        return None
    return max(found, key=lambda stall: stall.at)


# ----- the book: incidents, waiters and probes in the execution store -------------


@dataclass(frozen=True)
class Incident:
    incident_id: str
    kind: str
    profile_key: str
    profile: Profile
    state: str
    opened_at: datetime
    notify_at: Optional[datetime]
    notified_at: Optional[datetime]
    next_probe_at: datetime
    resets_at: Optional[datetime]
    last_result: Optional[Dict[str, Any]]

    @classmethod
    def from_row(cls, row: Any) -> "Incident":
        return cls(
            incident_id=row["incident_id"],
            kind=row["kind"],
            profile_key=row["profile_key"],
            profile=Profile.from_json(json.loads(row["profile_json"])),
            state=row["state"],
            opened_at=_parse(row["opened_at"]) or utcnow(),
            notify_at=_parse(row["notify_at"]),
            notified_at=_parse(row["notified_at"]),
            next_probe_at=_parse(row["next_probe_at"]) or utcnow(),
            resets_at=_parse(row["resets_at"]),
            last_result=json.loads(row["last_result_json"]) if row["last_result_json"] else None,
        )


@dataclass(frozen=True)
class Waiter:
    incident_id: str
    run_id: str
    project_id: str
    task_id: str
    session_id: str
    stall_at: datetime
    status: str
    nudges: int
    detail: Dict[str, Any]
    joined_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "Waiter":
        return cls(
            incident_id=row["incident_id"],
            run_id=row["run_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            stall_at=_parse(row["stall_at"]) or utcnow(),
            status=row["status"],
            nudges=int(row["nudges"]),
            detail=json.loads(row["detail_json"] or "{}"),
            joined_at=_parse(row["joined_at"]) or utcnow(),
        )


@dataclass(frozen=True)
class Joined:
    incident: Incident
    waiter: Waiter
    new_waiter: bool
    rearmed: bool


class IncidentBook:
    """Every transition on the three tables, each one short transaction."""

    def __init__(self, store: ExecutionStore) -> None:
        self.store = store

    # -- reads -------------------------------------------------------------------

    def incident(self, incident_id: str) -> Optional[Incident]:
        rows = self.store.read("SELECT * FROM auth_incident WHERE incident_id = ?", (incident_id,))
        return Incident.from_row(rows[0]) if rows else None

    def open_incidents(self) -> List[Incident]:
        rows = self.store.read(
            "SELECT * FROM auth_incident WHERE state = 'open' ORDER BY opened_at"
        )
        return [Incident.from_row(row) for row in rows]

    def incidents(self, *, limit: int = 50) -> List[Incident]:
        rows = self.store.read(
            "SELECT * FROM auth_incident ORDER BY opened_at DESC LIMIT ?", (int(limit),)
        )
        return [Incident.from_row(row) for row in rows]

    def waiters(self, incident_id: str) -> List[Waiter]:
        rows = self.store.read(
            "SELECT * FROM auth_waiter WHERE incident_id = ? ORDER BY joined_at, run_id",
            (incident_id,),
        )
        return [Waiter.from_row(row) for row in rows]

    def active_waiter_for_run(self, run_id: str) -> Optional[Waiter]:
        rows = self.store.read(
            "SELECT w.* FROM auth_waiter w JOIN auth_incident i USING (incident_id) "
            "WHERE w.run_id = ? AND w.status IN ('waiting','nudging','nudged','uncertain') "
            "ORDER BY w.joined_at DESC",
            (run_id,),
        )
        return Waiter.from_row(rows[0]) if rows else None

    def probes(self, profile_key: str) -> List[Dict[str, Any]]:
        rows = self.store.read(
            "SELECT * FROM auth_probe WHERE profile_key = ? ORDER BY started_at", (profile_key,)
        )
        return [
            {
                "probe_id": row["probe_id"],
                "incident_id": row["incident_id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
            }
            for row in rows
        ]

    # -- joining -----------------------------------------------------------------

    def join(
        self,
        *,
        kind: str,
        profile: Profile,
        run_id: str,
        project_id: str,
        task_id: str,
        session_id: str,
        stall: Stall,
        now: datetime,
    ) -> Joined:
        """Put a run's stall under its profile's open incident, opening one if needed.

        Idempotent on ``(run, stall.at)``. A *different* stall for a run already waiting
        is a fresh failure after a nudge: the waiter is re-armed, but the incident's next
        probe is never brought forward, which is what keeps a session that fails again
        straight after its nudge from turning into a nudge every poll.
        """
        policy = POLICIES[kind]
        key = profile.key
        stamp = _iso(now)
        with self.store.transaction("auth-join") as connection:
            row = connection.execute(
                "SELECT * FROM auth_incident WHERE kind = ? AND profile_key = ? AND state = 'open'",
                (kind, key),
            ).fetchone()
            if row is None:
                incident_id = f"inc_{uuid.uuid4().hex[:16]}"
                first_probe = _first_probe(kind, policy, stall, now)
                notify_at = (
                    now + timedelta(seconds=policy.notify_after)
                    if policy.notify_after is not None
                    else None
                )
                connection.execute(
                    "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, "
                    "state, opened_at, notify_at, next_probe_at, resets_at, updated_at) "
                    "VALUES (?,?,?,?,'open',?,?,?,?,?)",
                    (
                        incident_id,
                        kind,
                        key,
                        json.dumps(profile.as_json(), sort_keys=True),
                        stamp,
                        _iso(notify_at) if notify_at else None,
                        _iso(first_probe),
                        _iso(stall.resets_at) if stall.resets_at else None,
                        stamp,
                    ),
                )
            else:
                incident_id = row["incident_id"]
                if kind == KIND_USAGE_LIMIT and stall.resets_at is not None:
                    known = _parse(row["resets_at"])
                    if known is None or stall.resets_at > known:
                        due = max(
                            _parse(row["next_probe_at"]) or now,
                            stall.resets_at + timedelta(seconds=RESET_GRACE_SECONDS),
                        )
                        connection.execute(
                            "UPDATE auth_incident SET resets_at = ?, next_probe_at = ?, "
                            "updated_at = ? WHERE incident_id = ?",
                            (_iso(stall.resets_at), _iso(due), stamp, incident_id),
                        )
            # A run moves between kinds (a login dies, then a quota refuses): the older
            # waiter is superseded rather than left to be nudged for the wrong reason.
            connection.execute(
                "UPDATE auth_waiter SET status = 'removed', updated_at = ?, "
                "detail_json = json_set(detail_json, '$.removed', ?) "
                "WHERE run_id = ? AND incident_id <> ? "
                "AND status IN ('waiting','nudging','nudged','uncertain')",
                (stamp, f"superseded by a {kind} stall at {_iso(stall.at)}", run_id, incident_id),
            )
            existing = connection.execute(
                "SELECT * FROM auth_waiter WHERE incident_id = ? AND run_id = ?",
                (incident_id, run_id),
            ).fetchone()
            new_waiter = existing is None
            rearmed = False
            if existing is None:
                connection.execute(
                    "INSERT INTO auth_waiter(incident_id, run_id, project_id, task_id, "
                    "session_id, stall_at, status, detail_json, joined_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,'waiting',?,?,?)",
                    (
                        incident_id,
                        run_id,
                        project_id,
                        task_id,
                        session_id,
                        _iso(stall.at),
                        json.dumps(
                            {"message": stall.message[:500], "resets_at": _iso(stall.resets_at)}
                            if stall.resets_at
                            else {"message": stall.message[:500]}
                        ),
                        stamp,
                        stamp,
                    ),
                )
            elif _parse(existing["stall_at"]) != stall.at and existing["status"] in (
                NUDGED,
                RECOVERED,
                WAITING,
            ):
                rearmed = True
                detail = json.loads(existing["detail_json"] or "{}")
                detail["message"] = stall.message[:500]
                detail.setdefault("stalls", []).append(_iso(stall.at))
                status = WAITING
                resumed_for = _parse(detail.get("resumed_for_reset"))
                if kind == KIND_USAGE_LIMIT and resumed_for is not None:
                    if stall.resets_at is None or stall.resets_at <= resumed_for:
                        status = ESCALATED
                        detail["escalate"] = "repeat_limit"
                if stall.resets_at is not None:
                    detail["resets_at"] = _iso(stall.resets_at)
                if int(existing["nudges"]) >= MAX_NUDGES_PER_RUN:
                    status = ESCALATED
                    detail["escalate"] = "nudge_cap"
                if existing["status"] == RECOVERED:
                    # Its handoff was cleared at the recovery; a new stall earns its own.
                    detail.pop("handoff_entry", None)
                connection.execute(
                    "UPDATE auth_waiter SET stall_at = ?, status = ?, detail_json = ?, "
                    "updated_at = ? WHERE incident_id = ? AND run_id = ?",
                    (_iso(stall.at), status, json.dumps(detail), stamp, incident_id, run_id),
                )
                if status == WAITING:
                    due = max(
                        _parse(
                            connection.execute(
                                "SELECT next_probe_at FROM auth_incident WHERE incident_id = ?",
                                (incident_id,),
                            ).fetchone()[0]
                        )
                        or now,
                        now + timedelta(seconds=policy.probe_period),
                    )
                    connection.execute(
                        "UPDATE auth_incident SET next_probe_at = ?, updated_at = ? "
                        "WHERE incident_id = ?",
                        (_iso(due), stamp, incident_id),
                    )
            incident_row = connection.execute(
                "SELECT * FROM auth_incident WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            waiter_row = connection.execute(
                "SELECT * FROM auth_waiter WHERE incident_id = ? AND run_id = ?",
                (incident_id, run_id),
            ).fetchone()
            return Joined(
                incident=Incident.from_row(incident_row),
                waiter=Waiter.from_row(waiter_row),
                new_waiter=new_waiter,
                rearmed=rearmed,
            )

    # -- waiters -----------------------------------------------------------------

    def update_waiter(
        self,
        incident_id: str,
        run_id: str,
        *,
        expect: Sequence[str],
        status: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
        add_nudge: bool = False,
        session_id: Optional[str] = None,
        now: datetime,
    ) -> Optional[Waiter]:
        """Compare-and-set on status. ``None`` when another process moved it first."""
        with self.store.transaction("auth-waiter") as connection:
            row = connection.execute(
                "SELECT * FROM auth_waiter WHERE incident_id = ? AND run_id = ?",
                (incident_id, run_id),
            ).fetchone()
            if row is None or row["status"] not in expect:
                return None
            merged = json.loads(row["detail_json"] or "{}")
            if detail:
                merged.update(detail)
            connection.execute(
                "UPDATE auth_waiter SET status = ?, detail_json = ?, nudges = nudges + ?, "
                "session_id = ?, updated_at = ? WHERE incident_id = ? AND run_id = ?",
                (
                    status or row["status"],
                    json.dumps(merged, sort_keys=True, default=str),
                    1 if add_nudge else 0,
                    session_id or row["session_id"],
                    _iso(now),
                    incident_id,
                    run_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM auth_waiter WHERE incident_id = ? AND run_id = ?",
                (incident_id, run_id),
            ).fetchone()
            return Waiter.from_row(updated)

    # -- probes --------------------------------------------------------------------

    def claim_probe(self, incident: Incident, *, now: datetime, timeout: float) -> Optional[str]:
        """Reserve the one probe a profile may run now, or ``None`` with the reason recorded.

        Two refusals, both enforced in the transaction rather than by the caller's
        manners: another probe for the same profile still in flight (one that started
        more than twice the timeout ago is abandoned, not in flight), and the hourly cap.
        """
        holder, _ = this_holder()
        stamp = _iso(now)
        with self.store.transaction("auth-probe-claim") as connection:
            row = connection.execute(
                "SELECT state, next_probe_at FROM auth_incident WHERE incident_id = ?",
                (incident.incident_id,),
            ).fetchone()
            if row is None or row["state"] != "open" or (_parse(row["next_probe_at"]) or now) > now:
                return None
            stale = _iso(now - timedelta(seconds=2 * timeout))
            flying = connection.execute(
                "SELECT 1 FROM auth_probe WHERE profile_key = ? AND finished_at IS NULL "
                "AND started_at > ?",
                (incident.profile_key, stale),
            ).fetchone()
            if flying is not None:
                return None
            hour_ago = _iso(now - timedelta(hours=1))
            recent = connection.execute(
                "SELECT started_at FROM auth_probe WHERE profile_key = ? AND started_at > ? "
                "ORDER BY started_at",
                (incident.profile_key, hour_ago),
            ).fetchall()
            if len(recent) >= PROBE_HOURLY_CAP:
                oldest = _parse(recent[0]["started_at"]) or now
                connection.execute(
                    "UPDATE auth_incident SET next_probe_at = ?, updated_at = ? "
                    "WHERE incident_id = ?",
                    (_iso(oldest + timedelta(hours=1)), stamp, incident.incident_id),
                )
                return None
            probe_id = f"probe_{uuid.uuid4().hex[:16]}"
            connection.execute(
                "INSERT INTO auth_probe(probe_id, incident_id, profile_key, holder, started_at) "
                "VALUES (?,?,?,?,?)",
                (probe_id, incident.incident_id, incident.profile_key, holder, stamp),
            )
            return probe_id

    def finish_probe(
        self,
        probe_id: str,
        incident_id: str,
        result: ProbeResult,
        *,
        now: datetime,
        next_probe_at: datetime,
    ) -> None:
        record = result.as_record()
        record["at"] = _iso(now)
        with self.store.transaction("auth-probe-finish") as connection:
            connection.execute(
                "UPDATE auth_probe SET finished_at = ?, result_json = ? WHERE probe_id = ?",
                (_iso(now), json.dumps(record), probe_id),
            )
            connection.execute(
                "UPDATE auth_incident SET last_result_json = ?, next_probe_at = ?, "
                "updated_at = ? WHERE incident_id = ?",
                (json.dumps(record), _iso(next_probe_at), _iso(now), incident_id),
            )

    # -- incidents -----------------------------------------------------------------

    def mark_notified(self, incident_id: str, *, now: datetime) -> bool:
        """Claim the incident's one notification. ``False`` when it was already sent."""
        with self.store.transaction("auth-notify") as connection:
            cursor = connection.execute(
                "UPDATE auth_incident SET notified_at = ?, updated_at = ? "
                "WHERE incident_id = ? AND notified_at IS NULL AND state = 'open'",
                (_iso(now), _iso(now), incident_id),
            )
            return cursor.rowcount == 1

    def close(self, incident_id: str, *, state: str, reason: str, now: datetime) -> None:
        with self.store.transaction("auth-close") as connection:
            connection.execute(
                "UPDATE auth_incident SET state = ?, closed_reason = ?, updated_at = ? "
                "WHERE incident_id = ? AND state = 'open'",
                (state, reason, _iso(now), incident_id),
            )


def _first_probe(kind: str, policy: KindPolicy, stall: Stall, now: datetime) -> datetime:
    if kind == KIND_AUTH:
        return now
    if kind == KIND_USAGE_LIMIT:
        if stall.resets_at is None:
            return now + timedelta(seconds=UNKNOWN_RESET_PROBE_SECONDS)
        return max(now, stall.resets_at + timedelta(seconds=RESET_GRACE_SECONDS))
    return now + timedelta(seconds=policy.probe_period)


def book_for(home: Path) -> IncidentBook:
    from agentjobs.dispatch.journal import journal

    return IncidentBook(journal(home))


def holds_run(home: Path, run_id: str) -> bool:
    """Whether a poll must leave this run alone: a nudge is mid-flight or its result unknown."""
    try:
        waiter = book_for(home).active_waiter_for_run(run_id)
    except ExecutionStoreError:
        return False
    return waiter is not None and waiter.status in HOLDING_STATUSES


def awaiting_confirmation(home: Path, run_id: str, stall_at: datetime) -> bool:
    """Whether this stall has already been nudged and its reply is still to come."""
    try:
        waiter = book_for(home).active_waiter_for_run(run_id)
    except ExecutionStoreError:
        return False
    return waiter is not None and waiter.status == NUDGED and waiter.stall_at == stall_at


# ----- parking: what a poll does when it finds a stall ------------------------------


def park(runner: "DispatchRunner", handle: "RunHandle", stall: Stall) -> Joined:
    """Join the run to its incident and say what the kind says to say, once."""
    now = runner.clock()
    book = book_for(runner.home)
    joined = book.join(
        kind=stall.kind,
        profile=profile_for(runner, handle),
        run_id=handle.run_id,
        project_id=runner.resolution.project_id,
        task_id=handle.task_id,
        session_id=handle.session_id or "",
        stall=stall,
        now=now,
    )
    meta = handle.directory.read_meta()
    fields: Dict[str, object] = {}
    if meta.get("status") != "parked":
        fields["status"] = "parked"
    if stall.kind == KIND_AUTH and meta.get("auth_stalled_at") != stall.at.isoformat():
        fields["auth_stalled_at"] = stall.at.isoformat()
    if stall.kind != KIND_AUTH and meta.get("limit_stalled_at") != stall.at.isoformat():
        fields["limit_stalled_at"] = stall.at.isoformat()
    if meta.get("auth_incident") != joined.incident.incident_id:
        fields["auth_incident"] = joined.incident.incident_id
    if fields:
        handle.directory.update_meta(**fields)

    manager = runner.manager
    waiter = joined.waiter
    if waiter.status == ESCALATED and "escalated_entry" not in waiter.detail:
        _escalate(book, manager, joined.incident, waiter, now=now)
        return joined
    if not (joined.new_waiter or joined.rearmed):
        return joined
    task = manager.get_task(handle.task_id)
    if task is not None and task.log:
        # Everything already on the task when the run parked is context, not a later word.
        waiter = (
            book.update_waiter(
                waiter.incident_id,
                waiter.run_id,
                expect=ACTIVE_STATUSES,
                detail={"log_high_water": max(entry.id for entry in task.log)},
                now=now,
            )
            or waiter
        )
    policy = POLICIES[stall.kind]
    if policy.park is not None:
        ball, reason = policy.park
        entry = _handoff(
            manager,
            waiter,
            ball=ball,
            reason=reason,
            prompt=_park_prompt(joined.incident, waiter, stall),
            action="park",
        )
        if entry is not None:
            book.update_waiter(
                waiter.incident_id,
                waiter.run_id,
                expect=ACTIVE_STATUSES,
                detail={"handoff_entry": entry},
                now=now,
            )
    else:
        _note(
            manager,
            waiter.task_id,
            (
                f"Session `{waiter.session_id}` stopped on an authentication failure at "
                f"{stall.at.isoformat()}. AgentJobs is probing the credential store and will "
                "resume the session itself when it answers; a person is asked only if it is "
                f"still refusing at {_iso(joined.incident.notify_at or now)}."
            ),
            {"incident": joined.incident.incident_id, "run_id": waiter.run_id, "action": "probing"},
        )
    return joined


def _park_prompt(incident: Incident, waiter: Waiter, stall: Stall) -> str:
    if incident.kind == KIND_USAGE_LIMIT:
        when = _iso(stall.resets_at) if stall.resets_at else "an unreported time"
        return (
            f"Session `{waiter.session_id}` hit its usage limit at {stall.at.isoformat()} "
            f'("{stall.message}"). The limit resets at {when}. AgentJobs will probe after the '
            "reset and resume this same session once, with its own saved options. Nothing to "
            "do unless you want it sooner."
        )
    return (
        f"Session `{waiter.session_id}` hit a spend limit at {stall.at.isoformat()} "
        f'("{stall.message}"). Only the account owner can raise it. AgentJobs re-probes '
        "every 15 minutes and resumes this same session once the model answers; no Answer "
        "or Dispatch is needed after the limit is raised."
    )


# ----- one tick -------------------------------------------------------------------


@dataclass(frozen=True)
class NudgeReceipt:
    state: str
    """``applied``, ``not_applied``, ``deferred`` (nothing done, nothing spent) or ``unknown``."""
    detail: str
    session_id: Optional[str] = None


class Nudger(Protocol):
    def nudge(self, session_id: str, message: str) -> NudgeReceipt:
        ...


@dataclass
class TickContext:
    home: Path
    registry: "ProjectRegistry"
    managers: Dict[str, "TaskManagerLike"]
    clock: Callable[[], datetime]
    probe: ProbeRunner
    nudger_for: Callable[["DispatchRunner"], Optional[Nudger]]
    lines: List[str] = field(default_factory=list)


def tick(
    home: Path,
    *,
    registry: Optional["ProjectRegistry"] = None,
    managers: Optional[Dict[str, "TaskManagerLike"]] = None,
    clock: Callable[[], datetime] = utcnow,
    probe: ProbeRunner = run_probe,
    nudger_for: Optional[Callable[["DispatchRunner"], Optional[Nudger]]] = None,
) -> List[str]:
    """Advance every open incident once. Never raises; returns report lines."""
    from agentjobs.projects import ProjectRegistry

    context = TickContext(
        home=home,
        registry=registry or ProjectRegistry(home=home),
        managers=managers if managers is not None else {},
        clock=clock,
        probe=probe,
        nudger_for=nudger_for or default_nudger,
    )
    try:
        book = book_for(home)
        incidents = book.open_incidents()
    except ExecutionStoreError as exc:
        return [f"auth recovery unavailable: {exc}"]
    for incident in incidents:
        try:
            _advance(context, book, incident)
        except ExecutionStoreError as exc:
            context.lines.append(f"{incident.incident_id}: {exc}")
    return context.lines


def _advance(context: TickContext, book: IncidentBook, incident: Incident) -> None:
    now = context.clock()
    for waiter in book.waiters(incident.incident_id):
        if waiter.status in ACTIVE_STATUSES:
            _reconcile_waiter(context, book, incident, waiter, now)
    active = [w for w in book.waiters(incident.incident_id) if w.status in ACTIVE_STATUSES]
    if not active:
        recovered = any(w.status == RECOVERED for w in book.waiters(incident.incident_id))
        book.close(
            incident.incident_id,
            state="recovered" if recovered else "closed",
            reason="every waiter recovered" if recovered else "no waiter remains",
            now=now,
        )
        context.lines.append(f"{incident.incident_id}: closed")
        return
    waiting = [w for w in active if w.status == WAITING]
    if not waiting:
        return
    fresh = book.incident(incident.incident_id) or incident
    _notify_if_due(context, book, fresh, waiting, now)
    fresh = book.incident(incident.incident_id) or incident
    if fresh.next_probe_at > now:
        return
    runner = _runner(context, waiting[0].project_id, observe=True)
    if runner is None:
        return
    request = ProbeRequest(
        executable=fresh.profile.executable,
        model=fresh.profile.model,
        cwd=context.home / "auth-probe",
        env=_probe_env(runner, fresh.profile),
    )
    probe_id = book.claim_probe(fresh, now=now, timeout=request.timeout)
    if probe_id is None:
        return
    result = context.probe(request)
    finished = context.clock()
    book.finish_probe(
        probe_id,
        fresh.incident_id,
        result,
        now=finished,
        next_probe_at=_next_probe(fresh, result, finished),
    )
    context.lines.append(f"{fresh.incident_id}: probe {result.klass.value}")
    if not result.ready:
        return
    for waiter in book.waiters(fresh.incident_id):
        if waiter.status == WAITING:
            _nudge(context, book, fresh, waiter, finished)


def _next_probe(incident: Incident, result: ProbeResult, now: datetime) -> datetime:
    policy = POLICIES[incident.kind]
    if result.klass is ProbeClass.USAGE_EXHAUSTED:
        if result.resets_at is not None and result.resets_at > now:
            return result.resets_at + timedelta(seconds=RESET_GRACE_SECONDS)
        return now + timedelta(
            seconds=max(policy.probe_period, 900.0)
            if incident.kind != KIND_USAGE_LIMIT
            else UNKNOWN_RESET_PROBE_SECONDS
        )
    return now + timedelta(seconds=policy.probe_period)


def _probe_env(runner: "DispatchRunner", profile: Profile) -> Dict[str, str]:
    environment = runner._environment()
    default_home = str(auth.claude_home())
    if profile.claude_home and profile.claude_home != default_home:
        environment[auth.CLAUDE_HOME_ENV] = profile.claude_home
    return environment


def _runner(context: TickContext, project_id: str, *, observe: bool) -> Optional["DispatchRunner"]:
    """A runner for the project, or ``None``. ``observe`` accepts a refused launch gate."""
    from agentjobs.dispatch.config import (
        DispatchError,
        assert_dispatch_permitted,
        resolve_for_observation,
    )
    from agentjobs.dispatch.runner import DispatchRunner
    from agentjobs.projects import ProjectError
    from agentjobs.store_factory import dispatch_manager_for

    try:
        project = context.registry.get(project_id)
    except ProjectError:
        context.lines.append(f"{project_id}: not in the registry; auth recovery cannot act")
        return None
    try:
        resolution = assert_dispatch_permitted(project_id, context.home)
    except DispatchError as refused:
        if not observe:
            context.lines.append(f"{project_id}: not nudging, launching is refused ({refused})")
            return None
        try:
            resolution = resolve_for_observation(project_id, context.home)
        except DispatchError as exc:
            context.lines.append(f"{project_id}: {exc}")
            return None
    manager = context.managers.get(project_id) or dispatch_manager_for(project)
    return DispatchRunner(
        manager=manager, resolution=resolution, project_root=project.root, home=context.home
    )


# ----- reconciling a waiter against the world -------------------------------------


def _reconcile_waiter(
    context: TickContext, book: IncidentBook, incident: Incident, waiter: Waiter, now: datetime
) -> None:
    from agentjobs.dispatch import journal as journal_module
    from agentjobs.dispatch.runner import TERMINAL_STATUSES, RunDirectory, runs_root

    directory = RunDirectory(path=runs_root(context.home) / waiter.run_id)
    meta = directory.read_meta()
    reason: Optional[str] = None
    if not meta:
        reason = "the run record is gone"
    elif journal_module.cancel_requested(context.home, waiter.run_id, meta=meta):
        reason = "a Stop was requested"
    elif journal_module.stand_down(context.home, waiter.run_id) is not None:
        reason = "the run is standing down for the scripted finish"
    elif str(meta.get("status")) in TERMINAL_STATUSES:
        reason = f"the run ended ({meta.get('status')})"
        if waiter.status in (WAITING, NUDGED):
            # A session that recovered and then finished its turn before this tick is a
            # recovery first: its handoff, if still current, is cleared rather than left.
            since = _parse(waiter.detail.get("nudged_at")) or waiter.stall_at
            ended = auth.activity_since(waiter.session_id, since, home=_claude_home_path(incident))
            if ended is not None and ended.real_reply:
                _recovered(context, book, incident, waiter, now, how="before its run settled")
                return
    if reason is not None:
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=ACTIVE_STATUSES,
            status=REMOVED,
            detail={"removed": reason},
            now=now,
        )
        context.lines.append(f"{waiter.run_id}: left {incident.incident_id}: {reason}")
        return

    home = _claude_home_path(incident)
    if waiter.status == NUDGING:
        intended = _parse((waiter.detail.get("nudge") or {}).get("intended_at")) or now
        if (now - intended).total_seconds() < NUDGE_LEASE_SECONDS:
            return
        _reconcile_lost_nudge(context, book, incident, waiter, intended, home, now)
        return
    if waiter.status == NUDGED:
        nudged_at = _parse(waiter.detail.get("nudged_at")) or now
        activity = auth.activity_since(waiter.session_id, nudged_at, home=home)
        still = read_stall(waiter.session_id, home=home, since=waiter.stall_at)
        if (
            activity is not None
            and activity.real_reply
            and (still is None or still.at <= nudged_at)
        ):
            _recovered(context, book, incident, waiter, now, how="after the nudge")
        elif (now - nudged_at).total_seconds() >= CONFIRM_SECONDS:
            updated = book.update_waiter(
                waiter.incident_id,
                waiter.run_id,
                expect=(NUDGED,),
                status=ESCALATED,
                detail={"escalate": "unconfirmed"},
                now=now,
            )
            if updated is not None:
                _escalate(book, _manager(context, waiter), incident, updated, now=now)
        return
    if waiter.status == WAITING:
        still = read_stall(waiter.session_id, home=home, since=None)
        if still is None or still.at < waiter.stall_at:
            activity = auth.activity_since(waiter.session_id, waiter.stall_at, home=home)
            if activity is not None and activity.real_reply:
                _recovered(context, book, incident, waiter, now, how="on its own")


def _reconcile_lost_nudge(
    context: TickContext,
    book: IncidentBook,
    incident: Incident,
    waiter: Waiter,
    intended: datetime,
    home: Optional[Path],
    now: datetime,
) -> None:
    """A nudge intent with no result: find out from the transcript, never by resending."""
    activity = auth.activity_since(waiter.session_id, intended, home=home)
    if activity is not None and (activity.user_message or activity.real_reply):
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(NUDGING,),
            status=NUDGED,
            add_nudge=True,
            detail={"nudged_at": _iso(intended), "reconciled": "the transcript shows it arrived"},
            now=now,
        )
        context.lines.append(f"{waiter.run_id}: lost nudge reconciled as delivered")
        return
    updated = book.update_waiter(
        waiter.incident_id,
        waiter.run_id,
        expect=(NUDGING,),
        status=UNCERTAIN,
        add_nudge=True,
        detail={"escalate": "delivery_uncertain"},
        now=now,
    )
    if updated is not None:
        _escalate(book, _manager(context, waiter), incident, updated, now=now)
        context.lines.append(f"{waiter.run_id}: nudge delivery uncertain; escalated once")


def _claude_home_path(incident: Incident) -> Optional[Path]:
    return Path(incident.profile.claude_home) if incident.profile.claude_home else None


def _manager(context: TickContext, waiter: Waiter) -> Optional["TaskManagerLike"]:
    supplied = context.managers.get(waiter.project_id)
    if supplied is not None:
        return supplied
    from agentjobs.projects import ProjectError
    from agentjobs.store_factory import dispatch_manager_for

    try:
        manager = dispatch_manager_for(context.registry.get(waiter.project_id))
    except ProjectError:
        return None
    context.managers[waiter.project_id] = manager
    return manager


def _recovered(
    context: TickContext,
    book: IncidentBook,
    incident: Incident,
    waiter: Waiter,
    now: datetime,
    *,
    how: str,
) -> None:
    from agentjobs.dispatch.runner import RunDirectory, runs_root

    updated = book.update_waiter(
        waiter.incident_id,
        waiter.run_id,
        expect=(WAITING, NUDGED),
        status=RECOVERED,
        detail={"recovered_at": _iso(now), "recovered": how},
        now=now,
    )
    if updated is None:
        return
    directory = RunDirectory(path=runs_root(context.home) / waiter.run_id)
    if directory.read_meta().get("status") == "parked":
        directory.update_meta(status="running")
    manager = _manager(context, waiter)
    if manager is not None:
        cleared = _clear_handoff_if_current(manager, updated, incident)
        _note(
            manager,
            waiter.task_id,
            (
                f"Session `{waiter.session_id}` is working again {how}: a real model reply "
                f"was recorded after the {incident.kind.replace('_', ' ')} stall at "
                f"{_iso(waiter.stall_at)}."
                + (" The recovery handoff was cleared." if cleared else "")
            ),
            {"incident": incident.incident_id, "run_id": waiter.run_id, "action": "recovered"},
        )
    context.lines.append(f"{waiter.run_id}: recovered {how}")


def _clear_handoff_if_current(
    manager: "TaskManagerLike", waiter: Waiter, incident: Incident
) -> bool:
    """Hand the ball back to the agent only if this module's handoff is still the newest.

    A later permission question, review request, hold or Stop is somebody else's word and
    stays exactly as it is.
    """
    entry_id = waiter.detail.get("handoff_entry")
    if not isinstance(entry_id, int):
        return False
    task = manager.get_task(waiter.task_id)
    if task is None or not task.is_open or task.ball is Ball.AGENT:
        return False
    newest = next((e for e in reversed(task.log) if e.type is LogEntryType.HANDOFF), None)
    if newest is None or newest.id != entry_id:
        return False
    try:
        manager.handoff(
            waiter.task_id,
            actor=ACTOR,
            ball=Ball.AGENT,
            ball_reason=BallReason.WORK,
            ball_prompt=(
                f"The {incident.kind.replace('_', ' ')} condition recorded in entry {entry_id} "
                f"cleared and session `{waiter.session_id}` is working again. Carry on."
            ),
            expected_revision=task.updated,
            data={
                MARKER: {
                    "incident": incident.incident_id,
                    "run_id": waiter.run_id,
                    "action": "cleared",
                    "clears": entry_id,
                }
            },
        )
    except Exception:  # noqa: BLE001 - a changed task is a later word, not an error
        return False
    return True


# ----- notifying ------------------------------------------------------------------


def _notify_if_due(
    context: TickContext,
    book: IncidentBook,
    incident: Incident,
    waiting: Sequence[Waiter],
    now: datetime,
) -> None:
    if incident.notify_at is None or now < incident.notify_at:
        return
    if incident.last_result and incident.last_result.get("class") == ProbeClass.READY.value:
        return
    if incident.notified_at is None:
        if not book.mark_notified(incident.incident_id, now=now):
            return
        context.lines.append(f"{incident.incident_id}: notified")
    tasks = sorted({f"{w.project_id}/{w.task_id}" for w in waiting})
    for waiter in waiting:
        if isinstance(waiter.detail.get("handoff_entry"), int):
            continue
        manager = _manager(context, waiter)
        if manager is None:
            continue
        ball, reason = POLICIES[incident.kind].park or (Ball.HUMAN, BallReason.INPUT)
        entry = _handoff(
            manager,
            waiter,
            ball=ball,
            reason=reason,
            prompt=_notify_prompt(incident, waiter, tasks),
            action="notify",
        )
        if entry is not None:
            book.update_waiter(
                waiter.incident_id,
                waiter.run_id,
                expect=(WAITING,),
                detail={"handoff_entry": entry},
                now=now,
            )


def _notify_prompt(incident: Incident, waiter: Waiter, tasks: Sequence[str]) -> str:
    last = incident.last_result or {}
    probed = (
        f"The newest probe ({last.get('at')}) said `{last.get('class')}`: {last.get('detail')}"
        if last
        else "No probe has completed yet."
    )
    shared = (
        f" The same incident (`{incident.incident_id}`) is holding {len(tasks)} tasks: "
        + ", ".join(tasks)
        + "."
        if len(tasks) > 1
        else ""
    )
    if incident.kind == KIND_AUTH:
        return (
            f"`auth_unavailable`: the credential store behind session `{waiter.session_id}` "
            f"has refused authentication since {_iso(incident.opened_at)}. {probed}\n\n"
            "**Run `claude auth login` in a terminal on this machine.** That is the only "
            "action needed: AgentJobs keeps probing every minute and resumes this session "
            f"itself once the model answers -- no Answer and no Dispatch.{shared}"
        )
    return (
        _park_prompt(
            incident,
            waiter,
            Stall(incident.kind, waiter.stall_at, str(waiter.detail.get("message") or "")),
        )
        + shared
    )


def _escalate(
    book: IncidentBook,
    manager: Optional["TaskManagerLike"],
    incident: Incident,
    waiter: Waiter,
    *,
    now: datetime,
) -> None:
    """The one handoff for a condition automation cannot resolve. Written once per cause."""
    if manager is None or "escalated_entry" in waiter.detail:
        return
    cause = str(waiter.detail.get("escalate") or "unknown")
    prompts = {
        "delivery_uncertain": (
            f"A resume message was being delivered to session `{waiter.session_id}` and "
            "AgentJobs cannot tell whether it arrived: its record of the attempt has no "
            "result and the transcript shows neither the message nor a reply. It has not "
            "sent it again. Attach to the session to see its state, or Stop the run to "
            "release the task."
        ),
        "unconfirmed": (
            f"Session `{waiter.session_id}` was resumed after a positive probe but has "
            f"written no model reply in {int(CONFIRM_SECONDS // 60)} minutes. Attach to see "
            "what it is doing, or Stop the run to release the task."
        ),
        "nudge_cap": (
            f"Session `{waiter.session_id}` has stopped on the same condition after "
            f"{MAX_NUDGES_PER_RUN} resumes, although a fresh probe answers every time. "
            "Something specific to this session is wrong; AgentJobs has stopped resuming it. "
            "Attach to it, or Stop the run and dispatch afresh."
        ),
        "repeat_limit": (
            f"Session `{waiter.session_id}` was resumed after its usage limit reset and was "
            "refused again for the same window. AgentJobs will not loop against the limit. "
            "Check the account's usage, then answer to resume."
        ),
    }
    entry = _handoff(
        manager,
        waiter,
        ball=Ball.HUMAN,
        reason=BallReason.INPUT,
        prompt=prompts.get(
            cause, f"Auth recovery for session `{waiter.session_id}` needs a person: {cause}."
        ),
        action="escalate",
    )
    book.update_waiter(
        waiter.incident_id,
        waiter.run_id,
        expect=(waiter.status,),
        detail={"escalated_entry": entry, "handoff_entry": entry},
        now=now,
    )


def _handoff(
    manager: Optional["TaskManagerLike"],
    waiter: Waiter,
    *,
    ball: Ball,
    reason: BallReason,
    prompt: str,
    action: str,
) -> Optional[int]:
    if manager is None:
        return None
    task = manager.get_task(waiter.task_id)
    if task is None or task.lifecycle is Lifecycle.CLOSED:
        return None
    updated = manager.handoff(
        waiter.task_id,
        actor=ACTOR,
        ball=ball,
        ball_reason=reason,
        ball_prompt=prompt,
        data={
            MARKER: {
                "incident": waiter.incident_id,
                "run_id": waiter.run_id,
                "action": action,
            }
        },
    )
    newest = next((e for e in reversed(updated.log) if e.type is LogEntryType.HANDOFF), None)
    return newest.id if newest is not None else None


def _note(
    manager: Optional["TaskManagerLike"], task_id: str, body: str, data: Dict[str, Any]
) -> None:
    if manager is None:
        return
    try:
        manager.add_log_entry(
            task_id, actor=ACTOR, type=LogEntryType.NOTE, body=body, data={MARKER: data}
        )
    except Exception:  # noqa: BLE001 - a note is evidence, never a reason to stop recovery
        pass


# ----- nudging ------------------------------------------------------------------


RECOVERY_STUB = (
    "This is the **same session you were already running on task `{task_id}`**, resumed in "
    "place by AgentJobs -- not a new one. Your previous turn stopped at {stall_at} on "
    '{cause}: "{message}". A fresh probe at {probe_at} got a real answer from the model, '
    "so the condition has cleared.\n\n"
    "Carry on exactly where you stopped: repeat the step that failed, and keep the "
    "worktree, branch and account of the task you already have. Do not start the task over."
    "{earlier}\n\n"
    "If what is on disk no longer matches your account of this task, say so on the task and "
    "hand the ball back rather than improvising."
)


def recovery_message(
    *,
    task_id: str,
    incident: Incident,
    waiter: Waiter,
    probe_at: datetime,
    earlier: Sequence[str],
    policy: str,
) -> str:
    causes = {
        KIND_AUTH: "an authentication failure",
        KIND_USAGE_LIMIT: "a usage limit",
        KIND_SPEND_LIMIT: "a spend limit",
    }
    rendered_earlier = ""
    kept = [message.strip() for message in earlier if message and message.strip()]
    if kept:
        rendered_earlier = (
            "\n\nWhile you were stopped, a person sent these messages. They still apply, "
            "oldest first:\n\n" + "\n\n---\n\n".join(message[:4000] for message in kept)
        )
    rendered = RECOVERY_STUB.format(
        task_id=task_id,
        stall_at=_iso(waiter.stall_at),
        cause=causes.get(incident.kind, incident.kind),
        message=str(waiter.detail.get("message") or "")[:300],
        probe_at=_iso(probe_at),
        earlier=rendered_earlier,
    )
    if policy:
        rendered = f"{rendered}\n\n{policy}"
    return rendered


def _buffered_messages(
    manager: "TaskManagerLike",
    task_id: str,
    meta: Mapping[str, object],
    config: Mapping[str, object],
) -> Tuple[List[str], Optional[int]]:
    """Human handoffs to the agent written since this run last received anything."""
    from agentjobs.dispatch.guards import actor_kind

    task = manager.get_task(task_id)
    if task is None:
        return [], None
    after = max(
        _as_int(meta.get("dispatch_entry_id")),
        _as_int(meta.get("delivered_through_entry")),
    )
    messages: List[str] = []
    last: Optional[int] = None
    for entry in task.log:
        if entry.type is not LogEntryType.HANDOFF or entry.id <= after:
            continue
        kind = actor_kind(dict(config), entry.actor)
        if kind is None or not kind.is_human:
            continue
        if (entry.data or {}).get("ball") != Ball.AGENT.value:
            continue
        messages.append(entry.body or "")
        last = entry.id
    return messages, last


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _blocking_word(task: Any, waiter: Waiter) -> Optional[str]:
    """A later word from somebody else that a nudge must not talk over, if there is one."""
    if task is None or not task.is_open:
        return "the task is closed"
    if task.ball is Ball.AGENT and task.ball_reason is BallReason.HOLD:
        return "the task is on hold"
    newest = next((e for e in reversed(task.log) if e.type is LogEntryType.HANDOFF), None)
    if newest is None or task.ball is Ball.AGENT:
        return None
    if MARKER in (newest.data or {}):
        return None
    high_water = waiter.detail.get("log_high_water")
    if isinstance(high_water, int) and newest.id <= high_water:
        # The session's own last handoff before it died (a review request, say) is the
        # state it was resumed into, not a later word. Judged by entry id, which the log
        # orders, rather than by comparing its timestamps with a transcript's.
        return None
    return f"entry {newest.id} moved the ball to {task.ball.value} after the stall"


def _nudge(
    context: TickContext, book: IncidentBook, incident: Incident, waiter: Waiter, probe_at: datetime
) -> None:
    from agentjobs.dispatch.runner import RunDirectory, runs_root

    now = context.clock()
    manager = _manager(context, waiter)
    if manager is None:
        return
    task = manager.get_task(waiter.task_id)
    blocked = _blocking_word(task, waiter)
    if blocked is not None:
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(WAITING,),
            status=REMOVED,
            detail={"removed": blocked},
            now=now,
        )
        context.lines.append(f"{waiter.run_id}: not nudged: {blocked}")
        return
    if waiter.nudges >= MAX_NUDGES_PER_RUN:
        updated = book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(WAITING,),
            status=ESCALATED,
            detail={"escalate": "nudge_cap"},
            now=now,
        )
        if updated is not None:
            _escalate(book, manager, incident, updated, now=now)
        return
    runner = _runner(context, waiter.project_id, observe=False)
    if runner is None:
        return
    nudger = context.nudger_for(runner)
    if nudger is None:
        context.lines.append(f"{waiter.run_id}: this runner has no supported nudge")
        return

    directory = RunDirectory(path=runs_root(context.home) / waiter.run_id)
    meta = directory.read_meta()
    try:
        from agentjobs.projects import ProjectError

        config = context.registry.get(waiter.project_id).load_config()
    except (ProjectError, Exception):  # noqa: BLE001 - no config means no human actors known
        config = {}
    earlier, delivered_through = _buffered_messages(manager, waiter.task_id, meta, config)
    message = recovery_message(
        task_id=waiter.task_id,
        incident=incident,
        waiter=waiter,
        probe_at=probe_at,
        earlier=earlier,
        policy=_policy_for(context.home, waiter.run_id),
    )
    payload_sha = hashlib.sha256(message.encode("utf-8")).hexdigest()
    nudge_id = f"nudge:{waiter.run_id}:{_iso(waiter.stall_at)}"
    claimed = book.update_waiter(
        waiter.incident_id,
        waiter.run_id,
        expect=(WAITING,),
        status=NUDGING,
        detail={
            "nudge": {
                "id": nudge_id,
                "intended_at": _iso(now),
                "payload_sha256": payload_sha,
                "delivers_entries_through": delivered_through,
            }
        },
        now=now,
    )
    if claimed is None:
        return
    receipt = nudger.nudge(waiter.session_id, message)
    done = context.clock()
    if receipt.state == "applied":
        # From the intent, not the launcher's return: a reply written while the launcher
        # was still answering is still a reply to this nudge.
        detail: Dict[str, Any] = {"nudged_at": _iso(now), "acknowledged": receipt.detail[:300]}
        if incident.kind == KIND_USAGE_LIMIT and waiter.detail.get("resets_at"):
            detail["resumed_for_reset"] = waiter.detail.get("resets_at")
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(NUDGING,),
            status=NUDGED,
            add_nudge=True,
            detail=detail,
            session_id=receipt.session_id,
            now=done,
        )
        fields: Dict[str, object] = {"auth_nudged_at": _iso(done)}
        if delivered_through is not None:
            fields["delivered_through_entry"] = delivered_through
        if receipt.session_id and receipt.session_id != waiter.session_id:
            fields["session_id"] = receipt.session_id
        directory.update_meta(**fields)
        _note(
            manager,
            waiter.task_id,
            (
                f"Resumed session `{receipt.session_id or waiter.session_id}` in place after a "
                f"positive probe at {_iso(probe_at)} ({receipt.detail[:200]}). Recovery is "
                "confirmed only when it writes a real reply."
            ),
            {
                "incident": incident.incident_id,
                "run_id": waiter.run_id,
                "action": "nudged",
                "nudge": nudge_id,
                "payload_sha256": payload_sha,
                "posture_delivered": bool(_policy_for(context.home, waiter.run_id)),
                "delivered_entries_through": delivered_through,
            },
        )
        context.lines.append(f"{waiter.run_id}: nudged")
    elif receipt.state == "deferred":
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(NUDGING,),
            status=WAITING,
            detail={"last_nudge_error": receipt.detail[:300]},
            now=done,
        )
        context.lines.append(f"{waiter.run_id}: nudge deferred: {receipt.detail}")
    elif receipt.state == "not_applied":
        book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(NUDGING,),
            status=WAITING,
            add_nudge=True,
            detail={"last_nudge_error": receipt.detail[:300]},
            now=done,
        )
        context.lines.append(f"{waiter.run_id}: nudge not applied: {receipt.detail}")
    else:
        updated = book.update_waiter(
            waiter.incident_id,
            waiter.run_id,
            expect=(NUDGING,),
            status=UNCERTAIN,
            add_nudge=True,
            detail={"escalate": "delivery_uncertain", "last_nudge_error": receipt.detail[:300]},
            now=done,
        )
        if updated is not None:
            _escalate(book, manager, incident, updated, now=done)
        context.lines.append(f"{waiter.run_id}: nudge outcome unknown: {receipt.detail}")


def _policy_for(home: Path, run_id: str) -> str:
    """The posture and push clause the execution was granted, verbatim (task-375)."""
    from agentjobs.dispatch.journal import journal

    try:
        store = journal(home)
        attempt = store.attempt(run_id)
        if attempt is None or not attempt.execution_id:
            return ""
        execution = store.execution(attempt.execution_id)
    except ExecutionStoreError:
        return ""
    if execution is None:
        return ""
    clause = execution.envelope.get("policy_clause")
    return clause if isinstance(clause, str) else ""


# ----- the Claude Code nudge adapter --------------------------------------------


WOKE = re.compile(r"woke session\s+([0-9a-f]{6,})", re.IGNORECASE)
COPY = re.compile(r"started a copy(?:\s+as\s+([0-9a-f]{6,}))?", re.IGNORECASE)


class ClaudeSessionNudger:
    """task-417 entry 8's verified wake, as an adapter with an honest receipt.

    Verified on Claude Code 2.1.269/2.1.270 on 2026-09-13: a full uuid resumed while the
    session is still running, or a short id, *starts a copy* instead of continuing it. So
    the order is fixed -- stop, observe no pid, resume by full uuid with no flags -- and a
    launcher that reports a copy is treated as not applied, with the copy stopped.

    **That whole sequence is now the fallback** (task-451, Claude Code 2.1.276). A session
    parked on an expired login still has a process, and a process can be sent a message;
    ``_in_place`` tries that first and the stop-and-resume runs only when it misses. The
    difference is not speed -- it is that the recovered session keeps the identity the
    incident was recorded against, so a recovery no longer ends by having to find the run
    it was recovering under a new id.
    """

    def __init__(
        self,
        prefix: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        quiesce_seconds: float = 30.0,
        poll_seconds: float = 1.0,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prefix = list(prefix)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.quiesce_seconds = quiesce_seconds
        self.poll_seconds = poll_seconds
        self._run = run
        self._sleep = sleep
        self._monotonic = monotonic

    def _call(
        self, arguments: Sequence[str], *, stdin: Optional[str] = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess:
        return self._run(
            [*self.prefix, *arguments],
            input=stdin,
            cwd=str(self.cwd),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def _row(self, session_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """``(readable, row)`` from the full listing; unreadable is never "gone"."""
        try:
            completed = self._call(["agents", "--json", "--all"])
        except (OSError, subprocess.SubprocessError):
            return False, None
        if completed.returncode != 0 or not (completed.stdout or "").strip():
            return False, None
        try:
            loaded = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False, None
        if isinstance(loaded, dict):
            loaded = loaded.get("agents") or loaded.get("sessions") or []
        for row in loaded if isinstance(loaded, list) else []:
            if isinstance(row, dict) and (
                row.get("id") == session_id or row.get("sessionId") == session_id
            ):
                return True, row
        return True, None

    def _in_place(self, full: str, message: str) -> Optional[NudgeReceipt]:
        """Deliver by message instead of by stop-and-resume, or ``None`` to fall through.

        **This is the path that should normally run** (task-451). A session parked on an
        expired login is *alive*: the stop-and-resume below destroys a working process,
        its session id and its row in agent view purely to get one sentence into it, and
        then the recovery has to re-find the run it was recovering. Sending the sentence
        costs a headless turn and changes nothing about the session.

        ``None`` rather than a failed receipt, deliberately: a miss here has spent nothing
        and must not be mistaken for an attempt, so the caller goes on to the stop-and-
        resume exactly as it did before this existed.
        """
        live = peers.find_live_session(full)
        if live is None:
            return None
        delivery = peers.send_peer_message(
            live, message, prefix=self.prefix, cwd=self.cwd, env=self.env, run=self._run
        )
        if not delivery.delivered:
            return None
        return NudgeReceipt(
            "applied",
            f"woke the session in place: {delivery.detail}",
            session_id=live.session_id[:8],
        )

    def nudge(self, session_id: str, message: str) -> NudgeReceipt:
        readable, row = self._row(session_id)
        if not readable:
            return NudgeReceipt("not_applied", "the session listing could not be read")
        if row is None:
            return NudgeReceipt("not_applied", "the session is not in the listing")
        full = row.get("sessionId")
        if not isinstance(full, str) or not full:
            return NudgeReceipt("not_applied", "the listing gives no full session id")
        if row.get("status") == "busy":
            # Something else already woke it -- a person attaching, say. Stopping a working
            # session to deliver "carry on" would interrupt exactly what recovery wants.
            return NudgeReceipt("deferred", "the session is busy; it is not stopped to be resumed")
        in_place = self._in_place(full, message)
        if in_place is not None:
            return in_place
        if row.get("pid"):
            try:
                self._call(["stop", session_id])
            except (OSError, subprocess.SubprocessError) as exc:
                return NudgeReceipt("not_applied", f"stop failed: {exc}")
            deadline = self._monotonic() + self.quiesce_seconds
            while True:
                readable, row = self._row(session_id)
                if readable and (row is None or not row.get("pid")):
                    break
                if self._monotonic() >= deadline:
                    return NudgeReceipt(
                        "not_applied",
                        f"the session still had a pid {int(self.quiesce_seconds)}s after stop; "
                        "resuming now would start a copy",
                    )
                self._sleep(self.poll_seconds)
        try:
            completed = self._call(["--bg", "--resume", full], stdin=message, timeout=120.0)
        except subprocess.TimeoutExpired:
            return NudgeReceipt("unknown", "the resume launcher did not return")
        except (OSError, subprocess.SubprocessError) as exc:
            return NudgeReceipt("not_applied", f"the resume launcher could not start: {exc}")
        said = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
        copy = COPY.search(said)
        if copy is not None:
            if copy.group(1):
                try:
                    self._call(["stop", copy.group(1)])
                except (OSError, subprocess.SubprocessError):
                    pass
            return NudgeReceipt("not_applied", f"the launcher started a copy: {said[:200]}")
        woke = WOKE.search(said)
        if completed.returncode == 0 and woke is not None:
            return NudgeReceipt("applied", said[:300], session_id=woke.group(1)[:8])
        if completed.returncode != 0:
            return NudgeReceipt(
                "not_applied", f"resume exited {completed.returncode}: {said[:200]}"
            )
        return NudgeReceipt("unknown", f"the launcher's answer was not recognised: {said[:200]}")


def default_nudger(runner: "DispatchRunner") -> Optional[Nudger]:
    from agentjobs.dispatch.config import RunnerDriver

    if runner.runner.driver is RunnerDriver.CODEX:
        return None
    return ClaudeSessionNudger(
        runner.executable_prefix(), cwd=runner.project_root, env=runner._environment()
    )


def incident_summary(home: Path, *, limit: int = 20) -> List[Dict[str, Any]]:
    """The query surface: each incident with its waiters and newest probe."""
    book = book_for(home)
    rows: List[Dict[str, Any]] = []
    for incident in book.incidents(limit=limit):
        rows.append(
            {
                "incident": incident.incident_id,
                "kind": incident.kind,
                "state": incident.state,
                "opened_at": _iso(incident.opened_at),
                "notified_at": _iso(incident.notified_at) if incident.notified_at else None,
                "next_probe_at": _iso(incident.next_probe_at),
                "resets_at": _iso(incident.resets_at) if incident.resets_at else None,
                "last_probe": incident.last_result,
                "model": incident.profile.model,
                "waiters": [
                    {
                        "run_id": w.run_id,
                        "task": f"{w.project_id}/{w.task_id}",
                        "status": w.status,
                        "nudges": w.nudges,
                    }
                    for w in book.waiters(incident.incident_id)
                ],
            }
        )
    return rows


__all__ = [
    "ClaudeSessionNudger",
    "IncidentBook",
    "NudgeReceipt",
    "Profile",
    "Stall",
    "awaiting_confirmation",
    "holds_run",
    "incident_summary",
    "park",
    "read_stall",
    "tick",
]
