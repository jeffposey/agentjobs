"""What each kind of principal may do, and whether a claimed actor agrees with it.

Task-329 to task-331 built the answer to *who is asking*. This module is the first thing
that makes the answer matter: it turns a :class:`~agentjobs.principals.Principal` into a
set of capabilities, and it refuses a body ``user``/``actor`` that disagrees with the
principal the request actually resolved to.

**What it replaces.** The review endpoints compared the submitted ``user`` against the
project's ``default_user``, and ``GET /api/projects`` publishes ``default_user`` for
every project. That is not a weak check, it is a circular one -- the API told you the
password and then asked for it (audit 2026-08-21, finding S-1). The comparison here is
against the principal the transport proved, which no response hands out.

**Keyed by kind, never by person.** There are three kinds and there is no fourth, so the
table below is complete by construction rather than by maintenance. Per-person
permissions are explicitly out of scope for the whole of task-066: the moment a
capability depends on *which* human is asking, this stops being a table and becomes a
role system, and the epic said no.

**Two humans, one row each, and they are identical.** ``owner`` (the person at this
machine) and ``tailnet`` (a remote caller the front door authenticated) hold everything.
That is deliberate and not an oversight: the difference between them is how their
identity was established, not what they are allowed to do, and narrowing ``tailnet``
would be an access policy rather than an authorization model. The proxy is where an
access policy belongs (task-244).

**A run's set is scoped, not merely small.** Membership in the set is only half of an
answer for a ``run``: a run that may close *any* task can still close somebody else's,
so the verbs that name a task are additionally required to name **its own**. See
:data:`OWN_TASK_ONLY`.

Nothing here raises or knows about HTTP. It answers with a :class:`Denial` carrying a
code and a sentence, and :mod:`agentjobs.api.authorization` decides what status that is
-- the same split :mod:`agentjobs.principals` makes, and for the same reason: the rule
should be testable without a transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional

from .actors import HUMAN, actor_kinds, human_identity
from .principals import Principal, PrincipalKind, Resolution


class Capability(str, Enum):
    """One thing a caller may be permitted to do.

    Coarse on purpose. A capability per route would be a role system with extra steps,
    and the audit's finding is not that the verbs are too broadly grouped -- it is that
    nothing was checked at all. Each member below is a group of routes that stand or
    fall together for every kind of principal.
    """

    TASK_CREATE = "task.create"
    """File a new task. Not task-scoped: a new task is nobody's yet."""

    TASK_EDIT = "task.edit"
    """Rewrite a task's content: PATCH, archive, mark a deliverable."""

    TASK_VERB = "task.verb"
    """Move a task through the workflow: promote, claim, release, handoff, close, log."""

    TASK_QUEUE = "task.queue"
    """Change where one task stands: queue-move, queue-keep, reprioritize."""

    TASK_REVIEW = "task.review"
    """The human review loop: approve, request changes, answer, redirect, hold, resume,
    reject. A run holds this in no circumstances -- it is the capability the audit found
    an agent could reach by typing a name it had read out of a GET."""

    DISPATCH = "dispatch.start"
    """Spend money: start a run on a task or a playbook, or cancel one."""

    DISPATCH_ADMIN = "dispatch.admin"
    """Enable or disable dispatch for a project -- gate 3 of the dispatch design's four."""

    PROJECT_ADMIN = "project.admin"
    """Register, initialise or inspect a directory on this machine as a project."""

    QUEUE_ADMIN = "queue.admin"
    """Repair or compact a whole band: corpus-wide writes that name no single task."""

    WEBHOOK_ADMIN = "webhook.admin"
    """Create, delete or fire a webhook, and read the HMAC secret back."""

    RUN_OUTPUT = "run.output"
    """Read a run's or a finish's captured output.

    The one *read* in this table, and it is here because the alternative was refusing it
    to runs outright. A transcript is everything a session printed while it worked; a run
    may see its own and no other's, and a human may see any.
    """


_HUMAN_SET: FrozenSet[Capability] = frozenset(Capability)
"""Everything. Written as "every member" rather than as a list so that adding a
capability grants it to humans automatically -- the constraint on this task is that a
human loses nothing, and a list would make that a thing somebody has to remember."""

_RUN_SET: FrozenSet[Capability] = frozenset(
    {
        Capability.TASK_CREATE,
        Capability.TASK_EDIT,
        Capability.TASK_VERB,
        Capability.TASK_QUEUE,
        Capability.RUN_OUTPUT,
    }
)
"""What a dispatched agent may do. Enumerated, so adding a capability denies it to runs
until somebody decides otherwise -- the opposite default from the human set, and the
right one for the side of the table that is a security boundary."""

GRANTS: Dict[PrincipalKind, FrozenSet[Capability]] = {
    PrincipalKind.OWNER: _HUMAN_SET,
    PrincipalKind.TAILNET: _HUMAN_SET,
    PrincipalKind.RUN: _RUN_SET,
}
"""The capability table. Every kind appears; a kind added without a row fails
``tests/test_capabilities.py`` rather than silently holding nothing or everything."""

OWN_TASK_ONLY: FrozenSet[Capability] = frozenset(
    {
        Capability.TASK_EDIT,
        Capability.TASK_VERB,
        Capability.TASK_QUEUE,
    }
)
"""Capabilities a ``run`` holds only against the task it was dispatched to work.

``TASK_QUEUE`` is in here, which is the one entry worth defending. ALLAGENTS.md tells an
agent that disagrees with the backlog's order to move the task it thinks should be first
-- but that instruction addresses a session *choosing* what to work on next, and a
dispatched run is given its task rather than choosing it. A run reordering other people's
work is the same class of act as closing it.

Not applied to humans: an owner reorders the backlog, which is the whole point of the
queue controls on the dashboard.
"""


@dataclass(frozen=True)
class Denial:
    """A refusal with a code to branch on and a sentence a person can act on.

    The sentence matters more than usual here. The likeliest cause of any of these is a
    misconfigured identity mapping rather than an attack, and a message that says only
    "forbidden" turns a five-second config fix into a debugging session.
    """

    code: str
    detail: str


NO_PRINCIPAL = "no_principal"
CAPABILITY_DENIED = "capability_denied"
WRONG_TASK = "wrong_task"
WRONG_RUN = "wrong_run"
ACTOR_MISMATCH = "actor_mismatch"
IDENTITY_UNRESOLVED = "identity_unresolved"


def granted(principal: Principal) -> FrozenSet[Capability]:
    """What this principal holds. Empty for a kind with no row, never everything."""
    return GRANTS.get(principal.kind, frozenset())


def authorize(
    resolution: Resolution,
    capability: Capability,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Optional[Denial]:
    """Say why this request may not do this, or ``None`` when it may.

    Takes the whole :class:`~agentjobs.principals.Resolution` rather than the principal,
    because *why* nothing resolved is part of the answer: "you presented a credential we
    rejected" and "you reached us from an address the front door does not control" are
    different sentences for whoever has to fix it, and the resolution already carries
    both.

    ``task_id`` and ``run_id`` are what the request addressed. They are only consulted
    for a ``run``, and only for the capabilities that :data:`OWN_TASK_ONLY` and
    :attr:`Capability.RUN_OUTPUT` scope -- a human is not scoped to anything.
    """
    principal = resolution.principal
    if principal is None:
        return Denial(
            code=(resolution.problem.value if resolution.problem else NO_PRINCIPAL),
            detail=resolution.detail
            or (
                "This request resolved to no principal, so there is nobody to check a "
                "capability against."
            ),
        )

    if capability not in granted(principal):
        return Denial(
            code=CAPABILITY_DENIED,
            detail=(
                f"{principal.describe()} may not {capability.value}. "
                + _why_not(principal, capability)
            ),
        )

    if not principal.is_run:
        return None

    if capability in OWN_TASK_ONLY and task_id is not None:
        own = principal.task_id or ""
        if own != task_id:
            return Denial(
                code=WRONG_TASK,
                detail=(
                    f"Run {principal.run_id} was dispatched to work "
                    f"{own or 'no task at all'} and addressed {task_id}. A run may act "
                    "on its own task and no other; ask the human who dispatched you to "
                    "act on that one, or dispatch a run at it."
                ),
            )

    if capability is Capability.RUN_OUTPUT and run_id is not None:
        if (principal.run_id or "") != run_id:
            return Denial(
                code=WRONG_RUN,
                detail=(
                    f"Run {principal.run_id} asked to read the output of {run_id}. A "
                    "run's transcript is everything that session printed, and a run may "
                    "read its own and no other's."
                ),
            )

    return None


def _why_not(principal: Principal, capability: Capability) -> str:
    """The second sentence of a capability refusal: what this kind is, and what to do."""
    if principal.is_run:
        return (
            "A dispatched run holds a deliberately smaller set than the person who "
            "dispatched it: it may work its own task and file new ones, and it may not "
            "approve a review, start or cancel a run, change dispatch configuration, "
            "register a project, or repair the queue. Those are acts a human signs for."
        )
    return (
        f"No row of the capability table grants {capability.value} to " f"{principal.kind.value}."
    )


def actor_disagreement(
    config: Dict[str, Any],
    resolution: Resolution,
    claimed: str,
    *,
    field: str = "actor",
    require_human: bool = False,
) -> Optional[Denial]:
    """Refuse a claimed actor that disagrees with the principal, or answer ``None``.

    The explicit ``actor``/``user`` field survives this task and is not going away:
    agents and scripts post directly and hold no session, and the field is how a write
    says whose name goes in an append-only log. What changes is that it is now a claim
    checked against the transport rather than a claim taken on trust.

    **The check is made on the axis the principal could lie about, and only there.**

    *   A ``run`` may never claim a ``kind: human`` id -- that is the impersonation the
        whole epic exists to close -- and may not claim a *different agent* either,
        because an entry saying ``codex`` decided something is a lie about the record
        that nothing later rewrites.
    *   A human principal may not claim a **different human**. It may claim an agent id,
        and that is not a hole: the CLI and the MCP server run as the person at this
        machine and legitimately attribute their writes to the tool. An agent-attributed
        entry is also strictly the weaker one -- it cannot clock a dispatch, which is
        what ``assert_human_clocked`` is for. Refusing it would have broken every local
        agent write and protected nothing.

    ``require_human=True`` is the review loop's stricter form: there the claim must be
    the acting human exactly, because "approved by" is the one field in this system whose
    whole content is which person said yes.

    An unconfigured project -- a fresh ``agentjobs init``, whose ``actors:`` is empty --
    has no vocabulary to place a claim in, so there is nothing here to disagree with and
    nothing is refused. That is the same allowance :func:`agentjobs.actors.validate_actor`
    makes, and for the same reason: refusing would make the product unusable before it is
    configured.
    """
    claimed = (claimed or "").strip()
    if not claimed:
        return None

    principal = resolution.principal
    if principal is None:
        return Denial(
            code=(resolution.problem.value if resolution.problem else NO_PRINCIPAL),
            detail=resolution.detail
            or (
                f"This request claimed {field}={claimed!r} and resolved to no principal, "
                "so there is nothing to check the claim against."
            ),
        )

    kinds = actor_kinds(config)
    claimed_kind = kinds.get(claimed)

    if principal.is_run:
        return _run_disagreement(principal, claimed, claimed_kind, field=field)

    identity = human_identity(config, principal)
    if require_human or claimed_kind == HUMAN:
        if not identity.ok:
            return Denial(code=IDENTITY_UNRESOLVED, detail=identity.detail)
        if claimed != identity.user:
            return Denial(
                code=ACTOR_MISMATCH,
                detail=(
                    f"This request claimed {field}={claimed!r}, and it resolved to "
                    f"{identity.user!r} ({principal.describe()}). A caller may not act "
                    "as another person. If that mapping is wrong, it is the machine's "
                    "identity registry that needs the correction, not this request."
                ),
            )
    return None


def _run_disagreement(
    principal: Principal,
    claimed: str,
    claimed_kind: Optional[str],
    *,
    field: str,
) -> Optional[Denial]:
    """The run half of :func:`actor_disagreement`, kept separate for its own reasons."""
    if claimed_kind == HUMAN:
        return Denial(
            code=ACTOR_MISMATCH,
            detail=(
                f"This request claimed {field}={claimed!r}, which this project "
                f"configures as a person, and it resolved to {principal.describe()}. A "
                "dispatched run is never recorded as a human, and no configuration "
                "changes that."
            ),
        )

    expected = principal.agent_id or ""
    if not expected:
        # The ledger could not say which agent this run is -- a meta written by hand, or
        # by a version predating the field. Unknown is not "any": the human claim above
        # is still refused, which is the property that matters, and refusing an agent
        # claim we cannot check would break runs that were started before this landed.
        return None
    if claimed != expected:
        return Denial(
            code=ACTOR_MISMATCH,
            detail=(
                f"This request claimed {field}={claimed!r}, and it resolved to "
                f"{principal.describe()}, which was dispatched as {expected!r}. A run "
                "writes as itself: the log is append-only, so an entry attributed to "
                "another agent is a claim nothing later can withdraw."
            ),
        )
    return None


__all__ = [
    "ACTOR_MISMATCH",
    "CAPABILITY_DENIED",
    "Capability",
    "Denial",
    "GRANTS",
    "IDENTITY_UNRESOLVED",
    "NO_PRINCIPAL",
    "OWN_TASK_ONLY",
    "WRONG_RUN",
    "WRONG_TASK",
    "actor_disagreement",
    "authorize",
    "granted",
]
