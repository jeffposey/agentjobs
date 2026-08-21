"""Where a dispatched agent is told AgentJobs is listening.

A dispatched agent's first act is to read its own task record over HTTP, and its last is
to write the result back. Both need an address, and the address is the one thing the
agent cannot discover for itself -- so if AgentJobs hands it the wrong one, the run's
failure mode is silence: it cannot read the task, so it cannot log why it stopped.

Until 2026-08-19 the address was a literal ``http://localhost:8765`` repeated as a
parameter default in the runner, the guards and the auto-dispatch trigger, and the HTTP
endpoint never passed anything, so every dispatch got it. On the machine this feature
was built for, 8765 is specifically the port nothing is supposed to be listening on --
the dashboard runs on 8876 and a second server on the CLI's default is a documented
hazard here. The default was not merely often wrong; it named the one address guaranteed
to be dead (task-154).

**One resolver, one default, three sources, in this order:**

1. an address the caller *knows*, because it is the server answering the request. The
   HTTP endpoint derives it from the socket the request arrived on and passes it down.
2. ``AGENTJOBS_API_BASE``, for a CLI dispatch in a terminal that knows where the server
   is and does not want to edit a file to say so.
3. ``api_base:`` in machine-local ``~/.agentjobs/dispatch.yaml``, which is where a
   machine's standing answer belongs -- beside the runners, in the file that already
   describes what this machine will execute.

Falling through all three gives :data:`DEFAULT_API_BASE`, unchanged, because a machine
that has said nothing about its address and serves on the CLI default is the one case
the old literal was right about.

The precedence is deliberate: the request-derived address wins because it is *observed*
rather than *declared*, and a declaration that disagrees with the socket answering the
call is stale by definition. Configuration outranks nothing except the fallback.

**Resolving is not the same as being right.** Sources 2, 3 and 4 are all claims about a
port, made by a file or an environment or by this module, and any of the three can be
stale or absent while resolving perfectly. Task-193 is that failure observed: every CLI
dispatch on the machine this was built for fell through to (4) and told three real runs
that AgentJobs was at ``:8765``, which nothing on that machine serves. Nothing in the
resolution was broken -- it did exactly what it says here -- and the runs survived only
because they read the task YAML off disk instead.

So :func:`probe_api_base` asks whether anything actually answers there, and the dispatch
gate in ``guards.py`` uses it to refuse a run whose address is silent. It is checked for
the *declared* sources only: an observed address arrived on the socket that is answering
this very call, so probing it would ask a question already answered.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from agentjobs.dispatch.config import DispatchConfigError, load_dispatch_config

DEFAULT_API_BASE = "http://localhost:8765"
"""The last resort, and the only place this string appears in the dispatch stack."""

API_BASE_ENV = "AGENTJOBS_API_BASE"
"""Environment override, matching ``AGENTJOBS_HOME`` and friends in ``projects.py``."""

_ANY_HOST = frozenset({"", "0.0.0.0", "::", "*"})
"""Bind addresses that are not addresses. A wildcard bind is reachable on loopback."""


def normalise_api_base(value: str) -> str:
    """Trim a configured address to the form the prompt and ``{api_base}`` carry.

    Only whitespace and trailing slashes: a caller that wrote ``http://host:8876/`` and
    a caller that wrote ``http://host:8876`` mean the same thing, and every consumer
    appends ``/api/...`` to it. Nothing else is corrected, because silently rewriting an
    address someone typed is how you end up with a value nobody can find in any file.
    """
    return value.strip().rstrip("/")


def api_base_from_server(host: Optional[str], port: Optional[int]) -> Optional[str]:
    """The address of the socket a request arrived on, or ``None`` if there isn't one.

    ``host``/``port`` come from ASGI ``scope["server"]``, which uvicorn fills in from the
    listening socket's own ``getsockname()``. That is the reason to prefer it over the
    ``Host`` header: this dashboard is published on a tailnet through a loopback proxy,
    so the header says ``agentjobs.tailfed1df.ts.net`` while the agent that has to use
    the address is running on this machine. The socket cannot be forwarded, rewritten or
    spoofed, and what it reports is exactly what a local process can connect to.

    A wildcard bind is rewritten to loopback for the same reason -- ``http://0.0.0.0:``
    is a valid thing to bind and not a valid thing to connect to.

    The scheme is always ``http``: AgentJobs' own server speaks plain HTTP and has no TLS
    options at all, so TLS here is always a proxy's, in front of this socket. Reading
    ``scope["scheme"]`` would pick up that proxy's ``X-Forwarded-Proto`` and produce
    ``https://127.0.0.1:8876``, which is an address nothing serves.
    """
    if port is None:
        return None
    resolved = "127.0.0.1" if (host or "") in _ANY_HOST else str(host)
    if ":" in resolved:  # IPv6 literal, which a URL must bracket
        resolved = f"[{resolved}]"
    return f"http://{resolved}:{port}"


def _config_api_base(home: Optional[Path] = None) -> Optional[str]:
    """``api_base:`` from this machine's dispatch config, or ``None``.

    A dispatch config that will not parse is not an error *here*: the gates in
    ``config.py`` refuse the run for that reason and say so far better than a resolver
    guessing at an address could. Returning ``None`` lets the refusal happen where it
    reads well.
    """
    try:
        config = load_dispatch_config(home)
    except DispatchConfigError:
        return None
    if config is None or not config.api_base:
        return None
    return config.api_base


def configured_api_base(home: Optional[Path] = None) -> Optional[str]:
    """The address this machine has declared, from the environment or dispatch config.

    Sources 2 and 3 only -- what the machine *says*, with no fallback. Callers that
    want the address to actually hand an agent want :func:`resolve_api_base`; this one
    is for the places that need to distinguish "declared" from "assumed".
    """
    from_env = os.environ.get(API_BASE_ENV)
    if from_env and from_env.strip():
        return normalise_api_base(from_env)
    return _config_api_base(home)


# ----- where the answer came from ---------------------------------------------


class ApiBaseSource(str, Enum):
    """Which of the four sources above produced the address in hand.

    Carried because the two useful sentences about a broken address are different
    sentences. "You declared this in a file and nothing answers there" points at a line
    someone can edit; "nothing on this machine declared an address, so this is the
    fallback" points at a line that does not exist yet. A bare address cannot tell them
    apart, and the person reading it is by definition the person who does not know.
    """

    OBSERVED = "observed"
    """The socket a request arrived on. Never wrong, and never needs checking."""

    ENVIRONMENT = "environment"
    """``AGENTJOBS_API_BASE`` in this process's environment."""

    CONFIG = "config"
    """``api_base:`` in ``~/.agentjobs/dispatch.yaml``."""

    FALLBACK = "fallback"
    """Nobody said anything, so :data:`DEFAULT_API_BASE`."""


@dataclass(frozen=True)
class ResolvedApiBase:
    """An address, and the source that knew it."""

    value: str
    source: ApiBaseSource

    def describe_source(self, home: Optional[Path] = None) -> str:
        """Where this came from, as something a reader can go and look at."""
        if self.source is ApiBaseSource.OBSERVED:
            return "the socket this request arrived on"
        if self.source is ApiBaseSource.ENVIRONMENT:
            return f"${API_BASE_ENV}"
        if self.source is ApiBaseSource.CONFIG:
            from agentjobs.dispatch.config import dispatch_config_path

            return f"api_base: in {dispatch_config_path(home)}"
        return "the built-in fallback -- nothing on this machine declared an address"


def resolve_api_base_detail(
    explicit: Optional[str] = None, *, home: Optional[Path] = None
) -> ResolvedApiBase:
    """:func:`resolve_api_base`, plus which source answered.

    The resolution order is not duplicated here -- this *is* the resolution, and
    ``resolve_api_base`` is the value half of it kept for the callers that only ever
    wanted a string.
    """
    if explicit and explicit.strip():
        return ResolvedApiBase(normalise_api_base(explicit), ApiBaseSource.OBSERVED)
    from_env = os.environ.get(API_BASE_ENV)
    if from_env and from_env.strip():
        return ResolvedApiBase(normalise_api_base(from_env), ApiBaseSource.ENVIRONMENT)
    from_config = _config_api_base(home)
    if from_config is not None:
        return ResolvedApiBase(from_config, ApiBaseSource.CONFIG)
    return ResolvedApiBase(DEFAULT_API_BASE, ApiBaseSource.FALLBACK)


def resolve_api_base(explicit: Optional[str] = None, *, home: Optional[Path] = None) -> str:
    """The address to tell a dispatched agent, from whichever source knows it."""
    return resolve_api_base_detail(explicit, home=home).value


# ----- does anything actually answer there? -----------------------------------

VERSION_PATH = "/api/version"
"""The cheapest endpoint that proves the thing listening is AgentJobs and not a stranger."""

PROBE_TIMEOUT_SECONDS = 5.0
"""Long enough that a busy server is not mistaken for a dead one.

Two failure shapes, and only one of them waits. A *closed* port -- the case this exists
to catch -- refuses in microseconds and never approaches this. A *filtered* one answers
nothing at all and would otherwise hang a dispatch on a TCP retry schedule measured in
minutes, so it needs a ceiling.

The ceiling is generous rather than tight because the two errors cost different
amounts. Waiting an extra few seconds before refusing is an inconvenience; refusing a
dispatch whose server was merely slow is a working machine told it is broken. And slow
is reachable here: ``/api/version`` does no work of its own, but this application's
routes are ``async`` functions that read task files inline, so a probe arriving during a
large read waits behind it on the event loop rather than being served beside it.
"""


@dataclass(frozen=True)
class ApiBaseProbe:
    """What was found at an address, in the three states worth telling apart."""

    api_base: str

    answered: bool
    """Something at that address spoke HTTP. False means nothing is listening."""

    is_agentjobs: bool
    """It answered *and* identified itself as AgentJobs.

    False on an answering address means something is there and it is not this
    application -- a different service on a port you have reused, or an AgentJobs old
    enough not to serve ``/api/version``. That is worth saying and is not worth refusing
    over, because the check cannot tell those two apart and only one of them is broken.
    """

    detail: str
    """One clause naming what happened, for a message a human reads."""


def probe_api_base(api_base: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> ApiBaseProbe:
    """Ask an address whether AgentJobs is there.

    Reachability is judged by *any* HTTP response, including a 404: the failure this
    guards against is an address with nothing behind it, and a server that answers is
    one an agent can at least talk to and be told it is wrong by. Identification is
    judged separately and more strictly, by ``/api/version`` returning this
    application's own shape.

    Proxies are bypassed deliberately. This address is almost always loopback, and a
    machine with ``HTTP_PROXY`` set in its environment would otherwise have the probe
    ask a proxy about ``127.0.0.1`` -- which answers for the proxy's loopback, not ours.
    """
    url = f"{normalise_api_base(api_base)}{VERSION_PATH}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            body = response.read(4096)
            status = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        return ApiBaseProbe(
            api_base=api_base,
            answered=True,
            is_agentjobs=False,
            detail=f"answered HTTP {exc.code} at {VERSION_PATH}, which AgentJobs does not",
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        cause = getattr(exc, "reason", exc)
        return ApiBaseProbe(
            api_base=api_base,
            answered=False,
            is_agentjobs=False,
            detail=f"nothing answered ({cause})",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
        recognised = isinstance(payload, dict) and "schema_version" in payload
    except (UnicodeDecodeError, json.JSONDecodeError):
        recognised = False
    if not recognised:
        return ApiBaseProbe(
            api_base=api_base,
            answered=True,
            is_agentjobs=False,
            detail=f"answered HTTP {status}, but not as AgentJobs",
        )
    return ApiBaseProbe(
        api_base=api_base,
        answered=True,
        is_agentjobs=True,
        detail=f"AgentJobs answered (schema v{payload.get('schema_version')})",
    )
