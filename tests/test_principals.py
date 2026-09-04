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

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import httpx
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
from agentjobs.front_door import SECRET_ENV
from agentjobs.principals import (
    FRONT_DOOR_HEADER,
    IDENTITY_HEADER,
    RUN_CREDENTIAL_HEADER,
    Principal,
    PrincipalKind,
    PrincipalSource,
    Problem,
    RunCredential,
    is_front_door,
    is_local,
    no_run_credentials,
    reset_run_credential_verifier,
    resolve_principal,
    run_credential_verifier,
    set_run_credential_verifier,
    was_forwarded,
)

LOOPBACK = "127.0.0.1"
REMOTE = "100.64.0.7"
"""A tailnet address. Remote as far as this application is concerned: the proxy
forwards to loopback, so anything arriving with a CGNAT source address reached the
socket directly rather than through the front door."""

RUN_TOKEN = "run-credential-that-verifies"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        port: int = probe.getsockname()[1]
    return port


PROXY_SECRET = "the-secret-only-the-front-door-holds"
"""What the proxy presents to prove it is the proxy (task-244).

Loopback is not enough on its own any more, and these tests say so twice: once by
passing this and getting a ``tailnet`` principal, and once by withholding it and getting
the owner instead."""


def proven(login: str, *, secret: str = PROXY_SECRET) -> dict:
    """The headers a request that came through the front door actually carries."""
    return {IDENTITY_HEADER: login, FRONT_DOOR_HEADER: secret}


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
        headers=proven("jeff@example.com"),
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
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
        headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN, **proven("jeff@example.com")},
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
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
        headers=proven("somebody@example.com"),
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
    without_header = resolve_principal(
        client_host=REMOTE,
        headers={},
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
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
        headers=proven("somebody@example.com"),
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
    assert resolution.principal is None
    assert resolution.problem is Problem.NO_PROVEN_IDENTITY


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.53", "::1", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"]
)
def test_loopback_in_every_spelling_is_local(host: str) -> None:
    """``::ffff:127.0.0.1`` is the one that bites.

    It is what a dual-stack socket reports for an IPv4 loopback connection, and
    ``IPv6Address.is_loopback`` is False for it. Getting this wrong would not fail
    loudly -- it would quietly stop believing the proxy on some machines and not
    others, which is the worst shape a trust bug can take.
    """
    assert is_local(host) is True
    assert is_front_door(host, {FRONT_DOOR_HEADER: PROXY_SECRET}, secret=PROXY_SECRET) is True


@pytest.mark.parametrize("host", [None, "", "testclient", "example.com", "not-an-address"])
def test_an_unrecognisable_origin_is_neither_local_nor_the_front_door(host: Optional[str]) -> None:
    assert is_local(host) is False
    assert is_front_door(host, {FRONT_DOOR_HEADER: PROXY_SECRET}, secret=PROXY_SECRET) is False


# ----- the front door proves it is the front door (task-244) -------------------


@pytest.mark.parametrize(
    "headers, secret, why",
    [
        ({}, PROXY_SECRET, "no proof presented"),
        ({FRONT_DOOR_HEADER: ""}, PROXY_SECRET, "an empty proof"),
        ({FRONT_DOOR_HEADER: "wrong"}, PROXY_SECRET, "the wrong secret"),
        ({FRONT_DOOR_HEADER: PROXY_SECRET[:-1]}, PROXY_SECRET, "a near miss"),
        ({FRONT_DOOR_HEADER: PROXY_SECRET}, None, "no secret configured at all"),
        ({FRONT_DOOR_HEADER: PROXY_SECRET}, "", "an empty configured secret"),
    ],
)
def test_loopback_alone_is_not_the_front_door(
    headers: dict, secret: Optional[str], why: str
) -> None:
    """The residual task-329 recorded, narrowed.

    Every row here is a *local* request -- the socket is loopback and cannot be faked
    across the network -- and none of them is the front door. The last two are the
    default on a machine that runs no proxy: with no secret configured nothing can be
    the front door, so the identity header is inert rather than authoritative.
    """
    assert is_front_door(LOOPBACK, headers, secret=secret) is False, why


def test_a_local_process_forging_the_identity_header_gets_the_owner_not_tailnet() -> None:
    """The escalation this task closes, stated as the attack rather than as the rule.

    A dispatched agent runs on this machine. Before the front-door proof it could set
    ``X-Tailscale-User`` and be resolved ``tailnet``, which holds every capability there
    is -- review, dispatch, project admin -- against ``run``'s five of eleven. It now
    resolves to whatever it would have been with no header: the owner.
    """
    forged = resolve_principal(
        client_host=LOOPBACK,
        headers={IDENTITY_HEADER: "jeff@example.com"},
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
    bare = resolve_principal(
        client_host=LOOPBACK, headers={}, verifier=fake_verifier, front_door_secret=PROXY_SECRET
    )
    assert forged == bare
    assert forged.principal is not None
    assert forged.principal.kind is PrincipalKind.OWNER
    assert forged.principal.login is None


def test_a_front_door_request_that_names_nobody_resolves_nothing() -> None:
    """It does not fall through to the owner, and that is the whole point.

    The proxy refuses a connection ``WhoIs`` cannot identify, so this is a front-door
    fault rather than an expected state. The caller behind it is remote, and handing a
    remote caller the machine owner's identity is the largest escalation in the system;
    a bug in the proxy must not be able to grant it.
    """
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers={FRONT_DOOR_HEADER: PROXY_SECRET},
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
    assert resolution.principal is None
    assert resolution.problem is Problem.NO_PROVEN_IDENTITY


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
    """An application that mints no credentials resolves no runs, and refuses rather
    than falling back.

    This was the assertion that task-329 was inert. It is now the assertion about the
    *default*: task-331 installs a real verifier over it in ``api/main.py``, so what the
    module ships with is what a bare import gets and what every ``reset`` restores. The
    property that matters is unchanged either way -- a credential nothing can verify
    resolves nothing at all, rather than silently becoming the owner.
    """
    assert no_run_credentials(RUN_TOKEN) is None
    reset_run_credential_verifier()
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


@pytest.fixture()
def front_door(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Configure the application with a front-door secret, as a live install has.

    Through the environment rather than a module slot, because that is the path
    ``agentjobs.front_door.current_secret`` actually takes at runtime -- injecting the
    value some other way would test a seam nothing uses.
    """
    monkeypatch.setenv(SECRET_ENV, PROXY_SECRET)
    yield PROXY_SECRET


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


def test_the_application_believes_a_proven_header_from_the_front_door(
    probe_route: str, front_door: str
) -> None:
    with client_for(LOOPBACK) as client:
        body = client.get(probe_route, headers=proven("jeff@example.com")).json()
    assert body["kind"] == "tailnet"
    assert body["login"] == "jeff@example.com"
    assert body["describe"] == "tailnet (jeff@example.com) via proven_header"


def test_the_application_ignores_an_identity_header_with_no_front_door_proof(
    probe_route: str, front_door: str
) -> None:
    """Over the real transport: a local process cannot name itself (task-244).

    The socket really is loopback here -- this is not a remote caller pretending -- and
    the header still buys nothing, because the request cannot present what only the
    proxy holds.
    """
    with client_for(LOOPBACK) as client:
        forged = client.get(probe_route, headers={IDENTITY_HEADER: "jeff@example.com"}).json()
        wrong = client.get(probe_route, headers=proven("jeff@example.com", secret="a-guess")).json()
    for body in (forged, wrong):
        assert body["kind"] == "owner"
        assert body["login"] is None


def test_the_application_believes_nothing_when_no_front_door_is_configured(
    probe_route: str,
) -> None:
    """The default on a machine that runs no proxy: the header is inert.

    ``front_door`` is deliberately not requested here. With no secret configured there
    is no front door, so even a request carrying both headers is only the owner.
    """
    with client_for(LOOPBACK) as client:
        body = client.get(probe_route, headers=proven("jeff@example.com")).json()
    assert body["kind"] == "owner"


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


# ----- a reported address is only the socket's when nothing claimed to relay it -----


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "203.0.113.9"},
        {"X-Forwarded-For": LOOPBACK},
        {"X-Real-IP": "203.0.113.9"},
        {"Forwarded": "for=203.0.113.9"},
    ],
)
def test_a_request_claiming_to_have_been_forwarded_is_not_local(headers: dict) -> None:
    """Stronger than task-329's "these headers are not consulted", and it has to be.

    Uvicorn consults them below the application: ``ProxyHeadersMiddleware`` is on by
    default and replaces ``scope["client"]`` from ``X-Forwarded-For`` for a request
    arriving on loopback. Not consulting them in this module is therefore not enough --
    the address handed to it has already been rewritten. So their presence disqualifies
    the request instead.
    """
    assert was_forwarded(headers) is True
    resolution = resolve_principal(
        client_host=LOOPBACK,
        headers=headers,
        verifier=fake_verifier,
        front_door_secret=PROXY_SECRET,
    )
    assert resolution.principal is None
    assert resolution.problem is Problem.NO_PROVEN_IDENTITY


def test_a_forwarded_request_cannot_be_the_front_door_either() -> None:
    """Even holding the secret. Otherwise the rule above would have an exception in it
    exactly where an attacker would want one."""
    headers = dict(proven("jeff@example.com"))
    headers["X-Forwarded-For"] = LOOPBACK
    assert is_front_door(LOOPBACK, headers, secret=PROXY_SECRET) is False
    assert (
        resolve_principal(
            client_host=LOOPBACK,
            headers=headers,
            verifier=fake_verifier,
            front_door_secret=PROXY_SECRET,
        ).principal
        is None
    )


def test_an_empty_forwarding_header_is_an_absent_one() -> None:
    assert was_forwarded({"X-Forwarded-For": "", "X-Real-IP": "  "}) is False


# ----- the same three questions, asked of a real server (task-244) -----------------


def test_a_served_process_answers_the_same_as_the_pure_rule(tmp_path: Path) -> None:
    """The layer both previous test layers could not reach.

    ``TestClient`` speaks ASGI directly, so it never runs uvicorn's own middleware --
    which is precisely why nobody noticed that the served application read
    ``X-Forwarded-For`` after task-329 decided it would not. This starts a real uvicorn
    and asks it the three questions over a socket.
    """
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "tasks").mkdir()
    port = free_port()

    env = dict(os.environ)
    env["AGENTJOBS_HOME"] = str(home)
    env["AGENTJOBS_PROJECT_ROOT"] = str(tmp_path)
    env["AGENTJOBS_TASKS_DIR"] = str(tmp_path / "tasks")
    env[SECRET_ENV] = PROXY_SECRET
    env.pop("AGENTJOBS_API_BASE", None)
    # This process may itself be inside a dispatched run; the server must not inherit a
    # credential, or every request below would resolve as that run.
    env.pop("AGENTJOBS_RUN_CREDENTIAL", None)

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentjobs.api.main:app", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://{LOOPBACK}:{port}/api/whoami"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:  # pragma: no cover - only on a very slow machine
            pytest.skip("the server did not start in time")

        bare = httpx.get(url, timeout=10).json()
        forwarded = httpx.get(url, headers={"X-Forwarded-For": "203.0.113.9"}, timeout=10).json()
        front_door = httpx.get(url, headers=proven("jeff@example.com"), timeout=10).json()
        forged = httpx.get(url, headers={IDENTITY_HEADER: "jeff@example.com"}, timeout=10).json()
    finally:
        server.terminate()
        server.wait(timeout=30)

    assert bare["kind"] == "owner", "the person at the machine"
    assert forwarded["kind"] is None, (
        "a request that claims to have been relayed was believed; uvicorn rewrote the "
        "peer address and the application took it"
    )
    assert front_door["kind"] == "tailnet" and front_door["login"] == "jeff@example.com"
    assert forged["kind"] == "owner", "an identity header with no front-door proof"
