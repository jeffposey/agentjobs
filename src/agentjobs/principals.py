"""Who is asking: exactly one principal per request, and how it was established.

This is the seam the account system (task-066) hangs off. Its decision entry is
binding, and the part that shapes this module is that **loopback is split in two**.
A dispatched agent runs on this machine, so it arrives on 127.0.0.1 exactly as the
person at the keyboard does. Trusting loopback wholesale would leave the
agent-impersonation chain intact -- an agent can ``POST .../dispatch`` claiming to be
Jeff -- while looking like a fix. So a run presents a credential and is a *different*
kind of caller from the owner, and the credential is checked first for that reason.

Three kinds, and no fourth:

``tailnet``
    A remote caller whose identity the front door proved. The tsnet proxy performs the
    authentication; this module only decides whether to believe what it says.
``owner``
    A caller on loopback holding no run credential. The person at the machine.
``run``
    A dispatched agent, holding a run-scoped credential. Names its run and its task,
    and is never a human.

**This module resolves; it does not refuse.** When nothing resolves it says so, with a
problem code and a sentence -- see :class:`Resolution`. What a route *does* about that is
:mod:`agentjobs.capabilities`, which turns a principal into a capability set, and
:mod:`agentjobs.api.authorization`, which enforces it on every mutating route (task-332).
The split is deliberate and worth keeping: the trust rule below has to be arguable on its
own terms, and a module that both decides who you are and what you may do makes each
half harder to read.

**It also does not map an identity to an actor**, and ``actor_id`` is ``None`` on every
principal this module builds. Task-330 landed the mapping and deliberately put it
elsewhere: which configured person a login is depends on the *project's* actor
vocabulary, and resolution here is project-agnostic by design -- one request, one
principal, before any handler knows what it is addressing. A resolved principal carries
the raw login in :attr:`Principal.login`, and :func:`agentjobs.actors.human_identity`
turns that into an actor id given a project's config. Guessing it here would write an
attribution nobody configured, which is the failure :mod:`agentjobs.actors` exists to
prevent.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Optional, Union

IDENTITY_HEADER = "x-tailscale-user"
"""The header the front door sets to name the caller it authenticated.

Named for the proxy that sets it rather than generically, so a reader can tell at a
glance which component is being trusted. The proxy does not set it yet -- that is
task-244 -- and this module is correct in the meantime because an absent header simply
does not resolve ``tailnet``.
"""

RUN_CREDENTIAL_HEADER = "x-agentjobs-run"
"""The header a dispatched run presents its credential in.

The credential is minted in :mod:`agentjobs.dispatch.credentials` (task-331); this
module defines the slot and the precedence, and landing them separately is why the
precedence was tested before there was anything to present.
"""


class PrincipalKind(str, Enum):
    """The three kinds of caller. There is no fourth, and no anonymous kind."""

    TAILNET = "tailnet"
    OWNER = "owner"
    RUN = "run"


class PrincipalSource(str, Enum):
    """How the identity was established, for the audit trail.

    Kept separate from :class:`PrincipalKind` because the kind says what the caller is
    and the source says why we believe it, and an audit trail that records only the
    first cannot answer "on what evidence".
    """

    RUN_CREDENTIAL = "run_credential"
    PROVEN_HEADER = "proven_header"
    LOOPBACK = "loopback"


class Problem(str, Enum):
    """Why no principal resolved.

    Structured rather than prose so task-332 can decide what each one means without
    matching on a sentence, and so the two are never conflated: a caller from off the
    machine with no proven identity is an ordinary unauthenticated request, while a run
    credential that does not verify is somebody presenting a credential we reject.
    """

    NO_PROVEN_IDENTITY = "no_proven_identity"
    UNVERIFIED_RUN_CREDENTIAL = "unverified_run_credential"
    EXPIRED_RUN_CREDENTIAL = "expired_run_credential"


@dataclass(frozen=True)
class RunCredential:
    """What a verified run credential proves: which run, working which task, as whom."""

    run_id: str
    task_id: str = ""
    agent: str = ""
    """The agent actor id the run was dispatched as. Never a ``kind: human`` id.

    Added by task-332, which checks a submitted ``actor`` against it: a run writing as
    another agent is an attribution nobody can undo, the log being append-only. Empty
    when the ledger cannot say -- a meta written by hand, or by a version that did not
    record it -- and an empty value means "unknown", never "any".
    """


@dataclass(frozen=True)
class Refusal:
    """A verifier saying *why* a credential proves nothing, rather than only that.

    A verifier may return ``None`` and be done with it; that is a forgery, and the
    sentence :func:`resolve_principal` writes for it is right. This exists for the one
    case where the distinction is worth keeping: a credential that is genuine and whose
    run has ended (task-331). "Somebody presented a forgery" and "a real run outlived its
    credential" are different events for an operator reading a log, and task-332 may want
    to answer them differently.

    Both outcomes stop resolution dead. **Neither ever falls through to ``owner``** --
    that is the invariant a refusal exists to make explicit, not to weaken.
    """

    problem: Problem
    detail: str = ""


@dataclass(frozen=True)
class Principal:
    """Exactly one caller, resolved before any handler runs."""

    kind: PrincipalKind
    source: PrincipalSource
    actor_id: Optional[str] = None
    """The configured actor this principal maps to, where a caller already knows it.

    ``None`` on everything this module builds, and ``None`` forever for ``run`` -- a run
    is not a human and must never be attributed as one. The mapping is a function of the
    principal *and* a project's actor vocabulary, so it lives in
    :func:`agentjobs.actors.human_identity` rather than here; the field remains for a
    caller that has resolved one and wants to carry it.
    """

    login: Optional[str] = None
    """The raw identity the front door proved, before any mapping. ``tailnet`` only."""

    run_id: Optional[str] = None
    task_id: Optional[str] = None

    agent_id: Optional[str] = None
    """The agent actor id a ``run`` was dispatched as. ``None`` on every other kind.

    Deliberately not :attr:`actor_id`, which is the *human* a principal maps to and is
    documented as ``None`` forever for a run. The two are different claims and folding
    them into one field would make "a run is never recorded as a person" a matter of
    reading the kind rather than a property of the record.
    """

    @property
    def is_run(self) -> bool:
        return self.kind is PrincipalKind.RUN

    @property
    def is_human(self) -> bool:
        """True for the two kinds that are a person. Not the same as ``not is_run``.

        Written as an explicit membership test rather than a negation so that adding a
        kind forces a decision here instead of silently classifying it as a person.
        """
        return self.kind in (PrincipalKind.TAILNET, PrincipalKind.OWNER)

    def describe(self) -> str:
        """One line naming the kind and the evidence, for a log or an audit entry.

        Parent acceptance ac-1 turns on an action's record being able to name which
        kind acted; this is the sentence it names it with.
        """
        detail = self.login or self.actor_id
        if self.is_run:
            detail = self.run_id or ""
            if self.task_id:
                detail = f"{detail} on {self.task_id}" if detail else self.task_id
        named = f" ({detail})" if detail else ""
        return f"{self.kind.value}{named} via {self.source.value}"


@dataclass(frozen=True)
class Resolution:
    """The answer for one request: a principal, or the reason there isn't one.

    Deliberately shaped like :class:`agentjobs.actors.Identity`. A resolution that
    failed carries a problem code and a sentence a person can act on, and it never
    carries a substitute principal -- an invented default is the defect task-064
    removed, and re-introducing it here would make every control above it decorative.
    """

    principal: Optional[Principal] = None
    problem: Optional[Problem] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.principal is not None

    def describe(self) -> str:
        """One line for the audit trail, whether or not anybody resolved."""
        if self.principal is not None:
            return self.principal.describe()
        problem = self.problem.value if self.problem else "unresolved"
        return f"no principal ({problem})"


RunCredentialVerifier = Callable[[str], Union[RunCredential, Refusal, None]]
"""Turns a presented credential into the run it proves, or says it proves nothing.

Three return shapes, widened from two by task-331: the credential, a :class:`Refusal`
naming why a genuine-looking credential is being rejected, or ``None`` for "this proves
nothing". A verifier written against the original ``Optional[RunCredential]`` signature
still satisfies this one -- return types are covariant -- so nothing that installed a
verifier before had to change.
"""


def no_run_credentials(presented: str) -> Optional[RunCredential]:
    """Verify nothing: the default, for an application that mints no credentials.

    With it installed no request can resolve ``run``, so precedence rule 1 never fires.
    That was the whole of production until task-331; today the API application installs
    :func:`agentjobs.dispatch.credentials.verify_run_credential` over it at import, and
    this remains what a bare import of this module, and every test that resets, gets.
    """
    return None


_verifier: RunCredentialVerifier = no_run_credentials


def set_run_credential_verifier(verifier: RunCredentialVerifier) -> None:
    """Install the function that verifies presented run credentials."""
    global _verifier
    _verifier = verifier


def reset_run_credential_verifier() -> None:
    """Restore the default verifier, which verifies nothing."""
    global _verifier
    _verifier = no_run_credentials


def run_credential_verifier() -> RunCredentialVerifier:
    """The installed verifier."""
    return _verifier


def is_front_door(client_host: Optional[str]) -> bool:
    """True when a request arrived by the path the front door controls.

    **This is the trust rule, and it is the part of this module worth reading twice.**
    The proxy terminates the tailnet's HTTPS and forwards to ``127.0.0.1``, so a
    request bearing a proven identity reaches the application on loopback. A request
    arriving any other way did not come through the front door, whatever its headers
    say, and its identity header is ignored rather than believed.

    A header that is trusted unconditionally is worse than no header at all: it turns a
    body field anyone could set into a header field anyone can set while looking
    authoritative. Bind the server to ``0.0.0.0`` -- which is a thing people do -- and
    an unconditional rule would let any host on the LAN name itself as any user.

    An unparseable or absent client address is not the front door. There is no
    circumstance in which "we could not tell where this came from" should mean "believe
    what it claims about itself".

    What this rule does *not* do is separate the proxy from any other local process:
    both are loopback, so a local process could present an identity header and be
    believed as ``tailnet``. That residual is deliberate and bounded -- both kinds are
    human, so nothing is escalated by it, and the local caller that actually matters is
    a dispatched agent, which rule 1 catches by credential rather than by path.
    Narrowing loopback further needs the proxy to prove it is the proxy, which is a
    proxy-side change and belongs to task-244.
    """
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        # Not an address at all -- a unix socket, a test harness's placeholder, a
        # hostname. Unrecognised is not trusted.
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        # ``::ffff:127.0.0.1`` is loopback arriving on a dual-stack socket, and
        # IPv6Address.is_loopback says False for it. Unwrap before asking.
        address = mapped
    return address.is_loopback


def _header(headers: Mapping[str, str], name: str) -> str:
    """One header by lowercase name, tolerating a case-sensitive mapping."""
    value = headers.get(name)
    if value is None:
        for key, candidate in headers.items():
            if key.lower() == name:
                value = candidate
                break
    return (value or "").strip()


def resolve_principal(
    *,
    client_host: Optional[str],
    headers: Mapping[str, str],
    verifier: Optional[RunCredentialVerifier] = None,
) -> Resolution:
    """Resolve exactly one principal for a request, or report that none did.

    The order is fixed, and each step is here for a reason that is not interchangeable
    with the others:

    1.  **Run credential.** First, because a dispatched agent arrives on loopback and
        would otherwise be indistinguishable from the person at the machine. A
        credential that is presented and does *not* verify resolves nothing -- it must
        not fall through to ``owner``, or presenting rubbish would be a way to be
        promoted from agent to person, which is the whole chain this epic closes. That
        holds whichever way the verifier says no: ``None`` for a forgery, a
        :class:`Refusal` for a genuine credential whose run has ended.
    2.  **Proven identity header**, believed only from the front door -- see
        :func:`is_front_door`.
    3.  **Loopback with no credential**: the owner.
    4.  **Anything else**: nothing resolves, and the absence is reported.

    Pure by construction -- it takes a host and a header mapping rather than a
    ``Request`` -- so the trust rule can be exercised without a transport, and so the
    HTTP tests are testing the wiring rather than re-testing the logic.
    """
    verify = verifier if verifier is not None else run_credential_verifier()

    presented = _header(headers, RUN_CREDENTIAL_HEADER)
    if presented:
        credential = verify(presented)
        if isinstance(credential, Refusal):
            return Resolution(problem=credential.problem, detail=credential.detail)
        if credential is None:
            return Resolution(
                problem=Problem.UNVERIFIED_RUN_CREDENTIAL,
                detail=(
                    "A run credential was presented and did not verify. It is not "
                    "treated as an absent credential: falling back would let any local "
                    "process trade a rejected credential for the owner's identity."
                ),
            )
        return Resolution(
            principal=Principal(
                kind=PrincipalKind.RUN,
                source=PrincipalSource.RUN_CREDENTIAL,
                run_id=credential.run_id,
                task_id=credential.task_id or None,
                agent_id=credential.agent or None,
            )
        )

    front_door = is_front_door(client_host)
    if front_door:
        login = _header(headers, IDENTITY_HEADER)
        if login:
            return Resolution(
                principal=Principal(
                    kind=PrincipalKind.TAILNET,
                    source=PrincipalSource.PROVEN_HEADER,
                    login=login,
                )
            )
        return Resolution(
            principal=Principal(kind=PrincipalKind.OWNER, source=PrincipalSource.LOOPBACK)
        )

    return Resolution(
        problem=Problem.NO_PROVEN_IDENTITY,
        detail=(
            f"The request arrived from {client_host or 'an unknown address'}, which is "
            "not the path the front door controls, and carried no run credential. Any "
            "identity header on it was ignored rather than believed."
        ),
    )
