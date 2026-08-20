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
"""

from __future__ import annotations

import os
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


def configured_api_base(home: Optional[Path] = None) -> Optional[str]:
    """The address this machine has declared, from the environment or dispatch config.

    A dispatch config that will not parse is not an error *here*: the gates in
    ``config.py`` refuse the run for that reason and say so far better than a resolver
    guessing at an address could. Returning ``None`` lets the refusal happen where it
    reads well.
    """
    from_env = os.environ.get(API_BASE_ENV)
    if from_env and from_env.strip():
        return normalise_api_base(from_env)
    try:
        config = load_dispatch_config(home)
    except DispatchConfigError:
        return None
    if config is None or not config.api_base:
        return None
    return config.api_base


def resolve_api_base(explicit: Optional[str] = None, *, home: Optional[Path] = None) -> str:
    """The address to tell a dispatched agent, from whichever source knows it."""
    if explicit and explicit.strip():
        return normalise_api_base(explicit)
    return configured_api_base(home) or DEFAULT_API_BASE
