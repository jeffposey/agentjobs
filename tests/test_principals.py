"""Who is asking: resolution, precedence, and the trust rule.

The tests worth reading first are the trust ones. Everything else here proves a mapping
from an input to an output; those prove that a header nobody authenticated is *not*
believed, which is the property that makes the other two kinds mean anything.

Two layers, deliberately. The pure functions are exercised directly, because the rule
they encode should be readable without a transport in the way; the HTTP tests then check
that the application really does hand the socket's address to that rule rather than
something a caller can rewrite. Testing only the second layer would let a change that
reads ``X-Forwarded-For`` pass, and testing only the first would not notice if nothing
were wired up at all.
"""

from __future__ import annotations

from typing import Iterator, Optional

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import (
    PRINCIPAL_STATE_ATTR,
    get_principal,
    get_principal_resolution,
    resolve_request_principal,
)
from agentjobs.api.main import app
from agentjobs.principals import (
    IDENTITY_HEADER,
    RUN_CREDENTIAL_HEADER,
    Principal,
    PrincipalKind,
    PrincipalSource,
    Problem,
    RunCredential,
    is_front_door,
    no_run_credentials,
    reset_run_credential_verifier,
    resolve_principal,
    run_credential_verifier,
    set_run_credential_verifier,
)

LOOPBACK = "127.0.0.1"
REMOTE = "100.64.0.7"
"""A tailnet address. Remote as far as this application is concerned: the proxy
forwards to loopback, so anything arriving with a CGNAT source address reached the
socket directly rather than through the front door."""

RUN_TOKEN = "run-credential-that-verifies"


def fake_verifier(presented: str) -> Optional[RunCredential]:
    """Stand in for task-331's minting, so precedence can be tested before it exists."""
    if presented == RUN_TOKEN:
        return RunCredential(run_id="run_43c2a909", task_id="task-329")
    return None


# ----- the three origins (ac-1) ------------------------------------------------


def test_a_verified_run_credential_resolves_the_run() -> None:
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN},
        verifier=fake_verifier,
    )
    assert resolution.ok
    principal = resolution.principal
    assert principal is not None
    assert principal.kind is PrincipalKind.RUN
    assert principal.source is PrincipalSource.RUN_CREDENTIAL
    assert principal.run_id == "run_43c2a909"
    assert principal.task_id == "task-329"
    assert principal.actor_id is None, "a run is not a human and must never map to one"


def test_a_proven_header_from_the_front_door_resolves_tailnet() -> None:
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={IDENTITY_HEADER: "jeff@example.com"},
        verifier=fake_verifier,
    )
    principal = resolution.principal
    assert principal is not None
    assert principal.kind is PrincipalKind.TAILNET
    assert principal.source is PrincipalSource.PROVEN_HEADER
    assert principal.login == "jeff@example.com"
    assert principal.actor_id is None, "mapping a login to an actor is task-330"


def test_bare_loopback_resolves_the_owner() -> None:
    resolution = resolve_principal(client_host=LOOPBACK, headers={}, verifier=fake_verifier)
    principal = resolution.principal
    assert principal is not None
    assert principal.kind is PrincipalKind.OWNER
    assert principal.source is PrincipalSource.LOOPBACK


def test_the_run_credential_wins_over_a_proven_header_and_over_loopback() -> None:
    """Precedence rule 1, which is the reason loopback is split at all.

    A dispatched agent arrives on the same address the person at the machine does. If
    the header or the address were consulted first, an agent would resolve to a human
    and every capability the epic gives a human would be its to use.
    """
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN, IDENTITY_HEADER: "jeff@example.com"},
        verifier=fake_verifier,
    )
    principal = resolution.principal
    assert principal is not None
    assert principal.kind is PrincipalKind.RUN
    assert principal.login is None


# ----- the trust rule (ac-2) ---------------------------------------------------


def test_an_identity_header_off_the_front_door_resolves_as_if_it_were_absent() -> None:
    """The rule this module exists to enforce, stated as the spec states it.

    Not "the header is refused" -- the request resolves to *exactly* what it would have
    resolved to with no header at all. Comparing the two resolutions rather than
    asserting a kind is what makes this test still meaningful if the off-front-door
    answer ever changes.
    """
    with_header = resolve_principal(
        client_host=REMOTE,
        headers={IDENTITY_HEADER: "somebody@example.com"},
        verifier=fake_verifier,
    )
    without_header = resolve_principal(client_host=REMOTE, headers={}, verifier=fake_verifier)
    assert with_header == without_header
    assert with_header.principal is None
    assert (
        "somebody@example.com" not in with_header.detail
    ), "an ignored header must not be echoed as though it identified anybody"


@pytest.mark.parametrize(
    "host",
    [
        REMOTE,
        "192.168.1.20",
        "10.0.0.5",
        "8.8.8.8",
        "::2",
        "testclient",
        "",
        None,
    ],
)
def test_only_the_front_door_can_yield_a_tailnet_principal(host: Optional[str]) -> None:
    """Including the ones that are not addresses at all.

    ``testclient`` is Starlette's placeholder and an empty or missing peer is what a
    unix socket or an odd deployment produces. "We could not tell where this came from"
    must never mean "believe what it claims about itself".
    """
    resolution = resolve_principal(
        client_host=host,
        headers={IDENTITY_HEADER: "somebody@example.com"},
        verifier=fake_verifier,
    )
    assert resolution.principal is None
    assert resolution.problem is Problem.NO_PROVEN_IDENTITY


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.53", "::1", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"]
)
def test_loopback_in_every_spelling_is_the_front_door(host: str) -> None:
    """``::ffff:127.0.0.1`` is the one that bites.

    It is what a dual-stack socket reports for an IPv4 loopback connection, and
    ``IPv6Address.is_loopback`` is False for it. Getting this wrong would not fail
    loudly -- it would quietly stop believing the proxy on some machines and not
    others, which is the worst shape a trust bug can take.
    """
    assert is_front_door(host) is True


@pytest.mark.parametrize("host", [None, "", "testclient", "example.com", "not-an-address"])
def test_an_unrecognisable_origin_is_not_the_front_door(host: Optional[str]) -> None:
    assert is_front_door(host) is False


# ----- reporting absence rather than defaulting (ac-3) -------------------------


def test_nothing_resolves_to_a_default_identity() -> None:
    resolution = resolve_principal(client_host=REMOTE, headers={}, verifier=fake_verifier)
    assert resolution.ok is False
    assert resolution.principal is None
    assert resolution.problem is Problem.NO_PROVEN_IDENTITY
    assert resolution.detail, "an absence has to say why, or nothing above it can report it"
    assert resolution.describe() == "no principal (no_proven_identity)"


def test_a_presented_credential_that_does_not_verify_resolves_nothing() -> None:
    """It must not fall back to the owner, which is what loopback would otherwise give.

    Falling back would mean any local process could trade a rejected credential for the
    person's identity -- a downgrade attack on the one boundary the epic adds.
    """
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={RUN_CREDENTIAL_HEADER: "forged"},
        verifier=fake_verifier,
    )
    assert resolution.principal is None
    assert resolution.problem is Problem.UNVERIFIED_RUN_CREDENTIAL


def test_the_default_verifier_verifies_nothing_so_no_request_resolves_a_run() -> None:
    """This task is inert, and this is the assertion that says so.

    Until task-331 mints credentials there is nothing to verify, so the default must
    reject rather than accept -- and a presented credential therefore resolves nothing
    at all rather than silently becoming the owner.
    """
    assert no_run_credentials(RUN_TOKEN) is None
    assert run_credential_verifier() is no_run_credentials
    resolution = resolve_principal(client_host=LOOPBACK, headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN})
    assert resolution.principal is None
    assert resolution.problem is Problem.UNVERIFIED_RUN_CREDENTIAL


def test_an_empty_header_is_an_absent_header() -> None:
    """A header set to whitespace claims nothing, and must not be read as a claim."""
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={IDENTITY_HEADER: "   ", RUN_CREDENTIAL_HEADER: ""},
    )
    principal = resolution.principal
    assert principal is not None
    assert principal.kind is PrincipalKind.OWNER


# ----- the installed verifier is a slot, not a decision ------------------------


def test_the_verifier_can_be_installed_and_reset() -> None:
    try:
        set_run_credential_verifier(fake_verifier)
        resolution = resolve_principal(
            client_host=LOOPBACK, headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN}
        )
        assert resolution.principal is not None
        assert resolution.principal.kind is PrincipalKind.RUN
    finally:
        reset_run_credential_verifier()
    assert run_credential_verifier() is no_run_credentials


# ----- naming the principal in a log or an audit entry -------------------------


def test_a_principal_describes_its_kind_and_its_evidence() -> None:
    """Parent ac-1 needs an action's record to name which kind acted."""
    run = Principal(
        kind=PrincipalKind.RUN,
        source=PrincipalSource.RUN_CREDENTIAL,
        run_id="run_43c2a909",
        task_id="task-329",
    )
    assert run.describe() == "run (run_43c2a909 on task-329) via run_credential"
    assert run.is_run and not run.is_human

    tailnet = Principal(
        kind=PrincipalKind.TAILNET,
        source=PrincipalSource.PROVEN_HEADER,
        login="jeff@example.com",
    )
    assert tailnet.describe() == "tailnet (jeff@example.com) via proven_header"
    assert tailnet.is_human and not tailnet.is_run

    owner = Principal(kind=PrincipalKind.OWNER, source=PrincipalSource.LOOPBACK)
    assert owner.describe() == "owner via loopback"
    assert owner.is_human


# ----- the wiring: every request through the real application ------------------


@pytest.fixture()
def probe_route() -> Iterator[str]:
    """A route that reports the principal the application resolved for the request.

    Registered on the real ``app`` and popped again afterwards, so these tests drive
    the real middleware stack rather than a hand-assembled one. ``FastAPI`` has no
    public removal, and leaving a diagnostic route behind would be visible to every
    other test in the session.
    """
    path = "/__principal_probe"

    def probe(request: Request) -> dict:
        cached = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
        resolution = get_principal_resolution(request)
        principal = get_principal(request)
        return {
            "middleware_ran": cached is not None,
            "describe": resolution.describe(),
            "kind": principal.kind.value if principal else None,
            "source": principal.source.value if principal else None,
            "login": principal.login if principal else None,
            "run_id": principal.run_id if principal else None,
            "problem": resolution.problem.value if resolution.problem else None,
        }

    app.get(path, include_in_schema=False)(probe)
    added = app.router.routes[-1]
    try:
        yield path
    finally:
        app.router.routes.remove(added)


def client_for(host: str) -> TestClient:
    """A client whose requests appear to arrive from ``host``.

    The peer address is the whole subject of these tests, so it is set explicitly
    rather than left at Starlette's ``testclient`` placeholder.
    """
    return TestClient(app, client=(host, 51000))


def test_the_application_resolves_the_owner_on_loopback(probe_route: str) -> None:
    with client_for(LOOPBACK) as client:
        body = client.get(probe_route).json()
    assert body["middleware_ran"] is True
    assert body["kind"] == "owner"
    assert body["source"] == "loopback"


def test_the_application_believes_a_proven_header_from_the_front_door(probe_route: str) -> None:
    with client_for(LOOPBACK) as client:
        body = client.get(probe_route, headers={IDENTITY_HEADER: "jeff@example.com"}).json()
    assert body["kind"] == "tailnet"
    assert body["login"] == "jeff@example.com"
    assert body["describe"] == "tailnet (jeff@example.com) via proven_header"


def test_the_application_ignores_an_identity_header_from_anywhere_else(probe_route: str) -> None:
    """ac-2 over the real transport, including the headers a proxy hop would add.

    ``X-Forwarded-For`` is here because it is the obvious wrong fix: if resolution ever
    starts reading a forwarded address instead of the socket, this passes a loopback
    value in from a remote peer and the test fails.
    """
    with client_for(REMOTE) as client:
        body = client.get(
            probe_route,
            headers={
                IDENTITY_HEADER: "somebody@example.com",
                "X-Forwarded-For": LOOPBACK,
                "X-Real-IP": LOOPBACK,
            },
        ).json()
    assert body["kind"] is None
    assert body["problem"] == "no_proven_identity"
    assert body["describe"] == "no principal (no_proven_identity)"


def test_the_application_resolves_a_run_when_a_credential_verifies(probe_route: str) -> None:
    set_run_credential_verifier(fake_verifier)
    try:
        with client_for(LOOPBACK) as client:
            body = client.get(probe_route, headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN}).json()
    finally:
        reset_run_credential_verifier()
    assert body["kind"] == "run"
    assert body["run_id"] == "run_43c2a909"
    assert body["source"] == "run_credential"


def test_resolution_without_the_middleware_still_answers() -> None:
    """The dependency resolves on the spot when nothing stashed an answer.

    A route reached through a partially-wired test application must not silently get a
    default; it gets a real resolution or a reported absence, the same as any other.
    """

    class _Peer:
        host = REMOTE

    class _Request:
        client = _Peer()
        headers: dict = {}

        class state:  # noqa: N801 - mimics starlette's attribute bag
            pass

    resolution = resolve_request_principal(_Request())  # type: ignore[arg-type]
    assert resolution.principal is None
    assert get_principal_resolution(_Request()).problem is Problem.NO_PROVEN_IDENTITY  # type: ignore[arg-type]
    assert get_principal(_Request()) is None  # type: ignore[arg-type]
