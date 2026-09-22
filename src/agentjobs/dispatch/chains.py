"""Chain authorisation: what a human agrees to when they authorise a loop (task-150).

A chain is a bounded sequence of dispatches against one task, driven by
:mod:`agentjobs.dispatch.loop`. This module owns the half that decides whether there is
one and what its bounds are; the driver owns the half that spends it.

**Everything here is resolved from the stored task.** Dispatch design section 2 says a
dispatch may not read its authority from a request body, and a loop makes that rule
sharper rather than softer: the driver re-reads the authorisation before every iteration,
so a bound carried in a process is a bound a restarted process can lose, and a bound
supplied in a call is not evidence of anything. :func:`live_chain` takes a ``Task`` and
nothing else for exactly that reason.

**The digest is the load-bearing part** (design L4). Anyone may write a ``check`` --
refusing that was measured as pure friction on the useful case -- so what stops an agent
moving the finish line is not a write restriction but the fact that the set was frozen
when a person agreed to it. :func:`check_digest` hashes the criteria's ``(id, check)``
pairs; the driver recomputes it every turn, and a mismatch stops the chain under its own
name. That also catches the honest version of the same accident: a task edited from
another surface while a chain is running.

**Three refusals, each under its own code**, and each of them is the design refusing a
loop that could not converge rather than a validation error:

``no_checks``
    Nothing on the task is machine-decidable, so the loop has no termination condition it
    does not control. Section 7: refused, never capped.
``already_passing``
    Every check passes already. There is nothing to converge on, and the only thing such
    a loop can do is change something that was correct.
``chain_already_live``
    One is already running. Two chains against one task would each dispatch into the
    other's iterations, and neither's iteration count would mean anything.
``bound_exceeds_ceiling``
    An iteration cap or wall-clock past the design's ceiling. Refused twice over -- here,
    under this code, so a caller can branch on it and a person is told the number; and on
    :class:`~agentjobs.models_v2.ChainAuthorizationData`, so a row past a ceiling cannot
    reach the store from any writer at all.

**Revocation is one act** and it is effective before the next iteration, because that is
the only moment a chain can be stopped at: an iteration is a dispatch and a dispatch is
somebody's session. ``~/.agentjobs/DISPATCH_DISABLED`` stops every chain on the machine
for the same reason and needs nothing from this module -- the driver re-checks the
sentinel through ``assert_dispatch_permitted`` before every spawn, which is asserted in
``tests/test_agent_loops.py`` rather than assumed.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from agentjobs import clock as dispatch_clock
from agentjobs.dispatch.checks import CheckReport, NoChecksError, evaluate_task
from agentjobs.models_v2 import (
    DEFAULT_CHAIN_ITERATIONS,
    DEFAULT_CHAIN_WALL_CLOCK_SECONDS,
    MAX_CHAIN_ITERATIONS,
    MAX_CHAIN_WALL_CLOCK_SECONDS,
    AcceptanceStatus,
    ChainAuthorizationData,
    LogEntry,
    LogEntryType,
    Task,
)
from agentjobs.projects import Project

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from agentjobs.manager import TaskManager

__all__ = [
    "AlreadyPassingError",
    "BoundExceedsCeilingError",
    "ChainAlreadyLiveError",
    "ChainAuthorization",
    "ChainRefused",
    "NoChecksToChainError",
    "UnknownChainError",
    "authorize_chain",
    "check_digest",
    "chain_history",
    "current_chain",
    "iteration_results",
    "live_chain",
    "new_chain_id",
    "revoke_chain",
    "settled_criteria",
    "unchecked_criteria",
]


# ----- refusals ---------------------------------------------------------------


class ChainRefused(Exception):
    """A chain that could have been authorised and must not be.

    ``reason`` is the stable code a caller renders under, exactly as
    ``DispatchRefused.reason`` is: an agent branching on a refusal cannot pattern-match an
    English sentence, and a caller that had to would eventually match the wrong one.
    """

    reason = "chain_refused"


class NoChecksToChainError(ChainRefused):
    """No criterion carries a ``check``, so the loop has no oracle."""

    reason = "no_checks"


class AlreadyPassingError(ChainRefused):
    """Every check already passes, so there is nothing to converge on."""

    reason = "already_passing"


class ChainAlreadyLiveError(ChainRefused):
    """A chain against this task is already authorised and not revoked."""

    reason = "chain_already_live"


class BoundExceedsCeilingError(ChainRefused):
    """A requested bound is past the ceiling the design sets for it."""

    reason = "bound_exceeds_ceiling"


class UnknownChainError(ChainRefused):
    """The named chain is not on this task's record."""

    reason = "unknown_chain"


# ----- the digest -------------------------------------------------------------

DIGEST_VERSION = "1"
"""Prefix on every digest, so a change to how one is computed is visible rather than
silent. A chain authorised under an older rule fails to match under a newer one, which is
the correct outcome: the thing the human agreed to can no longer be shown to be what is
there now."""


def check_digest(task: Task) -> str:
    """A digest over the criteria's ``(id, check)`` pairs, in the task's own order.

    **What it covers is deliberately narrow.** The criterion's id and its argv, and
    nothing else -- not the prose, not the status, not the order's absence. Editing a
    criterion's ``text`` is an ordinary clarification and should not stop a running
    chain; editing its ``check`` changes what "done" executes, which is the one move this
    exists to catch. Adding or removing a checked criterion changes the digest too,
    because the set of things that must pass is what was agreed.

    Order *is* covered, by construction: the pairs are hashed in the task's own order and
    not sorted. Reordering acceptance criteria does not change what has to pass, so this
    is stricter than it needs to be -- and stricter in the direction that stops a chain
    and asks a person, which is the direction the whole design errs in.

    A criterion with no check contributes nothing. It was never part of the termination
    condition (L6), so a chain must not stop because somebody added a prose criterion
    while it ran.
    """
    digest = hashlib.sha256()
    digest.update(DIGEST_VERSION.encode("utf-8"))
    for criterion in task.acceptance:
        if not criterion.check:
            continue
        digest.update(b"\x00")
        digest.update(criterion.id.encode("utf-8"))
        for argument in criterion.check:
            digest.update(b"\x01")
            digest.update(argument.encode("utf-8"))
    return digest.hexdigest()


def checked_criteria(task: Task) -> List[str]:
    """The ids of criteria the digest covers, in the task's own order."""
    return [criterion.id for criterion in task.acceptance if criterion.check]


def new_chain_id() -> str:
    """A fresh chain id. Short, random, and never derived from the task.

    Derived ids were considered and rejected: a second chain against one task, authorised
    after the first was revoked, would collide with it, and every entry either one wrote
    would then read as belonging to both.
    """
    return f"chain_{secrets.token_hex(4)}"


# ----- reading an authorisation off the record --------------------------------


@dataclass(frozen=True)
class ChainAuthorization:
    """One chain as the stored task describes it, with nothing added.

    Constructed only by :func:`live_chain` and :func:`chain_history`, both of which read
    the log. There is no constructor that takes bounds from a caller, and that absence is
    the forgeability rule: a driver cannot be handed an authorisation, only shown where to
    read one.
    """

    entry: LogEntry
    data: ChainAuthorizationData
    revoked_by_entry: Optional[LogEntry] = None

    @property
    def chain_id(self) -> str:
        return self.data.chain_id

    @property
    def authorized_at(self) -> datetime:
        """When the human authorised it, always tz-aware."""
        stamp = self.entry.ts
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    @property
    def deadline(self) -> datetime:
        """When the wall-clock bound expires."""
        return self.authorized_at + timedelta(seconds=self.data.wall_clock_seconds)

    @property
    def revoked(self) -> bool:
        return self.revoked_by_entry is not None

    def expired(self, now: Optional[datetime] = None) -> bool:
        """Whether the wall-clock bound has run out."""
        return (now or dispatch_clock.utcnow()) >= self.deadline

    def matches(self, task: Task) -> bool:
        """Whether the task's checks are still the ones this authorised."""
        return check_digest(task) == self.data.check_digest

    def describe(self) -> str:
        """One line naming the chain and its bounds, for a log body or a prompt."""
        hours = self.data.wall_clock_seconds / 3600
        return (
            f"`{self.chain_id}`: at most {self.data.max_iterations} iteration(s) within "
            f"{hours:.1f}h, against {len(self.data.criteria)} checked criteria"
        )


def _authorization_of(entry: LogEntry) -> Optional[ChainAuthorizationData]:
    """Parse one ``chain_authorized`` entry, or ``None`` when it is not one.

    A row that cannot be parsed answers ``None`` rather than raising. The model makes
    that state unreachable through every write path here; this is what happens to a row
    that reached the store some other way, and treating it as *no authorisation* is the
    only safe reading -- the alternative is a loop running on bounds nobody can read.
    """
    if entry.type is not LogEntryType.CHAIN_AUTHORIZED:
        return None
    payload = {key: value for key, value in (entry.data or {}).items() if key != "operation"}
    try:
        return ChainAuthorizationData.model_validate(payload)
    except Exception:  # noqa: BLE001 - an unparseable row is not an authorisation
        return None


def _revocations(task: Task) -> dict[str, LogEntry]:
    """Every revoked chain id on this record, by id."""
    revoked: dict[str, LogEntry] = {}
    for entry in task.log:
        if entry.type is not LogEntryType.CHAIN_REVOKED:
            continue
        chain_id = str((entry.data or {}).get("chain_id") or "").strip()
        if chain_id:
            revoked.setdefault(chain_id, entry)
    return revoked


def chain_history(task: Task) -> List[ChainAuthorization]:
    """Every chain ever authorised against this task, oldest first."""
    revoked = _revocations(task)
    found: List[ChainAuthorization] = []
    for entry in task.log:
        data = _authorization_of(entry)
        if data is None:
            continue
        found.append(
            ChainAuthorization(entry=entry, data=data, revoked_by_entry=revoked.get(data.chain_id))
        )
    return found


def current_chain(task: Task) -> Optional[ChainAuthorization]:
    """The newest chain nobody has revoked, whatever its clock says.

    **This is what the driver reads, and the clock's absence is the point.** Every other
    stop condition -- the iteration cap, the digest, the wall-clock -- is something the
    driver has to be able to *see* in order to stop under its own name. A function that
    made an expired chain vanish would turn "the four hours you agreed to are up" into
    "there is no chain here", and a person reading the handoff could not tell that from a
    revocation. Section 8's rule is that every stop is loud; a stop the driver cannot name
    is not.

    :func:`live_chain` is the other question -- *may another chain be authorised* -- and
    that one does consult the clock.
    """
    for chain in reversed(chain_history(task)):
        if chain.revoked:
            continue
        return chain
    return None


def live_chain(task: Task, *, now: Optional[datetime] = None) -> Optional[ChainAuthorization]:
    """The chain that is running right now: authorised, not revoked, not expired.

    What a surface asks to decide whether to offer a Revoke button, and what
    :func:`authorize_chain` asks before refusing a second chain -- an authorisation whose
    four hours have passed is not one a new chain would collide with.

    It deliberately does **not** mean "has iterations left" or "the digest still matches".
    Those are the driver's stop conditions, and :func:`current_chain` is what the driver
    reads for exactly that reason.
    """
    chain = current_chain(task)
    if chain is None or chain.expired(now or dispatch_clock.utcnow()):
        return None
    return chain


def iteration_results(task: Task, chain_id: str) -> List[Tuple[int, List[Tuple[str, str]]]]:
    """This chain's result vectors, oldest first, as ``(iteration, [(id, status)])``.

    The vector is what the guardrails compare -- not the diff, per section 8 -- so it is
    produced here once and read by the driver, the CLI and the panel's API rather than
    reassembled three times.
    """
    vectors: List[Tuple[int, List[Tuple[str, str]]]] = []
    for entry in task.log:
        if entry.type is not LogEntryType.CHECK_RESULT:
            continue
        data = entry.data or {}
        if str(data.get("chain_id") or "") != chain_id:
            continue
        iteration = data.get("iteration")
        results = data.get("results") or []
        vectors.append(
            (
                int(iteration) if iteration is not None else 0,
                [(str(item.get("id")), str(item.get("status"))) for item in results],
            )
        )
    return vectors


# ----- authorising ------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationOutcome:
    """A chain that was authorised, with the baseline pass that justified it."""

    task: Task
    chain: ChainAuthorization
    baseline: CheckReport


def authorize_chain(
    *,
    manager: "TaskManager",
    project: Project,
    task: Task,
    actor: str,
    max_iterations: int = DEFAULT_CHAIN_ITERATIONS,
    wall_clock_seconds: int = DEFAULT_CHAIN_WALL_CLOCK_SECONDS,
    home: Optional[Path] = None,
    chain_id: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AuthorizationOutcome:
    """Authorise a bounded chain against this task's checks, or refuse and say why.

    **The baseline pass is run before anything is written, and then recorded as iteration
    zero.** It has to run: "every check already passes" cannot be answered from the
    stored statuses, which were set by whatever last ran and may be hours old. And having
    run it, throwing it away would be worse than wasteful -- it is precisely the vector
    the regression guard needs to compare iteration one against. So the chain's history
    starts at zero with the state of the world at the moment somebody agreed to it.

    ``actor`` is the human the authorisation is attributed to. Whether that id *is* a
    human is not decided here: the API route resolves it through
    ``guards.assert_authorizer_is_human`` before calling, and the capability gate has
    already refused a run principal outright. This function is reachable from the CLI,
    which is served as the owner, and from that route. Neither hands it a claim it has
    not already validated -- which is the same split ``dispatch_task`` makes, and for the
    same reason: a rule tested without a transport is a rule.

    The bounds are checked by the payload model, so a value past a ceiling is refused
    identically from every caller.
    """
    moment = now or dispatch_clock.utcnow()

    # Checked here as well as on the payload model, and the duplication is the point: the
    # model refuses the *value*, which is what stops a bad row reaching the store from
    # any writer, and this refuses the *request* under a code a caller can branch on. A
    # pydantic ValidationError is not a reason code, and a person who typed 50 should be
    # told the ceiling rather than shown a schema error.
    if not 1 <= max_iterations <= MAX_CHAIN_ITERATIONS:
        raise BoundExceedsCeilingError(
            f"{max_iterations} iterations is outside the permitted range "
            f"1-{MAX_CHAIN_ITERATIONS}. Past the ceiling a chain is not converging, it "
            "is wandering, and the difference matters more than the extra attempts."
        )
    if not 1 <= wall_clock_seconds <= MAX_CHAIN_WALL_CLOCK_SECONDS:
        hours = MAX_CHAIN_WALL_CLOCK_SECONDS / 3600
        raise BoundExceedsCeilingError(
            f"{wall_clock_seconds}s is outside the permitted range 1s-{hours:.0f}h. The "
            "ceiling is what makes a chain started after dinner one that has stopped by "
            "morning."
        )

    existing = live_chain(task, now=moment)
    if existing is not None:
        raise ChainAlreadyLiveError(
            f"{task.id} already has a live chain -- {existing.describe()}, authorised at "
            f"{existing.authorized_at.isoformat()}. Revoke it before authorising "
            "another: two chains against one task would each dispatch into the other's "
            "iterations, and neither's count would bound anything."
        )

    try:
        baseline = evaluate_task(task, project=project, home=home)
    except NoChecksError as exc:
        raise NoChecksToChainError(
            f"{task.id} has no acceptance criterion with a `check`, so a chain against it "
            "would have no termination condition the agent does not control. Add a "
            "`check` argv to the criteria the loop is meant to settle."
        ) from exc

    if baseline.ok:
        passing = ", ".join(outcome.id for outcome in baseline.results)
        raise AlreadyPassingError(
            f"Every check on {task.id} already passes ({passing}), so there is nothing "
            "for a chain to converge on. The only thing such a loop can do is change "
            "something that was already correct."
        )

    identifier = chain_id or new_chain_id()
    digest = check_digest(task)
    criteria = checked_criteria(task)
    failing = ", ".join(outcome.id for outcome in baseline.failed)
    hours = wall_clock_seconds / 3600
    body = (
        f"{actor} authorised chain `{identifier}` against {task.id}: at most "
        f"{max_iterations} iteration(s) within {hours:.1f}h, against "
        f"{len(criteria)} checked criteria ({', '.join(criteria)}).\n\n"
        f"Failing at authorisation: {failing}. The checks are frozen as they stand now "
        f"(digest `{digest[:12]}`); changing any of them stops the chain."
    )
    if note:
        body = f"{body}\n\n{note}"

    written = manager.record_chain_authorization(
        task.id,
        actor=actor,
        chain_id=identifier,
        max_iterations=max_iterations,
        wall_clock_seconds=wall_clock_seconds,
        check_digest=digest,
        criteria=criteria,
        body=body,
    )

    # Iteration zero: the state of the world the human agreed to converge from. Written
    # after the authorisation so the two read in the right order and the vector has an
    # authorisation to belong to.
    written = manager.record_check_result(
        written.id,
        actor=actor,
        results=baseline.results,
        unchecked=baseline.unchecked,
        chain_id=identifier,
        iteration=0,
        body=(
            f"Iteration 0 of {max_iterations} (baseline at authorisation): "
            f"{len(baseline.results) - len(baseline.failed)} of {len(baseline.results)} "
            "checks pass."
        ),
    )

    chain = live_chain(written, now=moment)
    if chain is None:  # pragma: no cover - the entry was just written
        raise ChainRefused(
            f"The authorisation for {identifier} was written to {task.id} and could not "
            "be read back. The record is the source of truth here, so nothing will run."
        )
    return AuthorizationOutcome(task=written, chain=chain, baseline=baseline)


# ----- revoking ---------------------------------------------------------------


def revoke_chain(
    *,
    manager: "TaskManager",
    task: Task,
    actor: str,
    chain_id: Optional[str] = None,
    note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Task:
    """Withdraw a chain's authorisation. One act, effective before the next iteration.

    ``chain_id`` defaults to whatever is live, which is what makes this one command with
    no arguments a person has to look up -- section 9 asks for a kill switch as blunt as
    ``agentjobs dispatch stop``, and one that needed an id copied off a log entry would
    not be one.

    Revoking an already-revoked chain is not an error: it is a person pressing stop twice
    on something they want stopped, and refusing the second press would be the tool
    arguing. It writes nothing the second time.

    **This does not stop a run that is already executing**, and nothing about it pretends
    to. An iteration is a dispatch and a dispatch is somebody's session; ``agentjobs
    dispatch cancel`` is what ends one of those. What this guarantees is that no
    *further* iteration begins, which is the bound the design asks for.
    """
    moment = now or dispatch_clock.utcnow()
    history = chain_history(task)
    if chain_id is None:
        live = live_chain(task, now=moment)
        if live is None:
            if history and history[-1].revoked:
                # Somebody pressed stop twice on something they want stopped. Refusing
                # the second press would be the tool arguing with a person about an
                # outcome they and it already agree on.
                return task
            raise UnknownChainError(
                f"{task.id} has no live chain to revoke. `agentjobs chain show "
                f"{task.id}` lists every chain it has had and what stopped each one."
            )
        target = live
    else:
        found = [item for item in history if item.chain_id == chain_id]
        if not found:
            known = ", ".join(item.chain_id for item in history) or "none"
            raise UnknownChainError(
                f"{task.id} has no chain {chain_id!r}. Chains on this task: {known}."
            )
        target = found[-1]

    if target.revoked:
        return task

    body = (
        f"{actor} revoked chain `{target.chain_id}`. No further iteration will start; a "
        "run already executing is not stopped by this -- cancel it if you need it to "
        "stop now."
    )
    if note:
        body = f"{body}\n\n{note}"
    return manager.record_chain_revocation(
        task.id,
        actor=actor,
        chain_id=target.chain_id,
        re=target.entry.id,
        body=body,
    )


def unchecked_criteria(task: Task) -> List[str]:
    """Criteria a chain never touches, named for the handoff that ends it.

    L6: the loop converges on the checked criteria and hands the rest to a person, and
    the prompt it hands over has to say which were which. A person told only that "the
    checks pass" does not know what they are being asked to judge.
    """
    return [criterion.id for criterion in task.acceptance if not criterion.check]


def settled_criteria(task: Task, results: Sequence[object]) -> List[str]:
    """Criteria this pass found ``met``, in the task's own order."""
    met = {
        str(getattr(item, "id", ""))
        for item in results
        if getattr(item, "status", None) is AcceptanceStatus.MET
    }
    return [criterion.id for criterion in task.acceptance if criterion.id in met]
