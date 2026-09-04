"""The shared secret that lets the tailnet proxy prove it is the tailnet proxy.

Task-329 established *who is asking* and left one residual written on its own record:
loopback does not separate the front door from any other process on this machine, so a
local process could set ``X-Tailscale-User`` and be believed as ``tailnet``. That is an
escalation and not a cosmetic one -- ``tailnet`` holds every capability, ``run`` holds
five of eleven (:mod:`agentjobs.capabilities`) -- so a dispatched agent that forged the
header would walk straight past the boundary task-332 built.

This module is the proxy-side half task-329 assigned to task-244. The proxy presents a
secret in :data:`agentjobs.principals.FRONT_DOOR_HEADER` on every request it forwards,
and the identity header is believed only when that secret matches.

**What it is worth, stated plainly, because overstating it would be the worse error.**
The proxy and every dispatched agent run as the same user on the same machine, so no
secret stored here can be *unreadable* by an agent determined to find it. What the
secret buys is three things that are real:

1.  Forging a tailnet identity stops being "set a header" and becomes "read a file you
    were not pointed at, then set two headers". Not a wall; a deliberate act rather than
    an accident, and one that leaves a shape in a transcript.
2.  An install with no proxy running cannot produce a ``tailnet`` principal **at all**,
    because there is no secret to match. Absent a front door, the header is inert.
3.  The trust boundary acquires a name and a file, so a future deployment that does
    separate the proxy -- another user, another container, another host -- tightens by
    changing where the secret lives rather than by redesigning resolution.

**The app only ever reads.** The proxy creates the file, because the proxy is the front
door; an application that minted the secret would be handing out the thing it uses to
decide whom to believe. A missing file is therefore not an error, it is the ordinary
state of a machine that runs no proxy.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Tuple

from .projects import default_home

SECRET_ENV = "AGENTJOBS_FRONT_DOOR_SECRET"
"""Environment override, checked before the file.

For a deployment that would rather inject the secret than share a path -- a container, a
service manager with a secret store -- and for tests, which set it and get an immediate
answer because the environment is consulted on every call.
"""

SECRET_FILENAME = "front-door-secret"
"""Name of the file under the machine-level AgentJobs home (``~/.agentjobs``).

The same path the Go proxy computes, and the coupling between the two is deliberately a
documented constant in both rather than an argument somebody has to remember to pass.
"""

_FILE_CACHE_SECONDS = 5.0
"""How long a file read is reused.

Not a performance decision -- the file is 65 bytes -- but an ordering one. Reading once
at import would mean a server started before the proxy never sees the secret and
silently treats every remote caller as the owner until somebody restarts it, which is
exactly the shape of footgun this repository keeps writing incident notes about. Five
seconds makes the start order irrelevant while keeping one stat per request off the
table.
"""

_cache: Optional[Tuple[float, Optional[str]]] = None


def secret_path() -> Path:
    """Where the shared secret lives when it is a file."""
    return default_home() / SECRET_FILENAME


def reset_cache() -> None:
    """Forget the cached file read. For tests, and for a caller that just wrote it."""
    global _cache
    _cache = None


def _read_file() -> Optional[str]:
    try:
        content = secret_path().read_text(encoding="utf-8").strip()
    except OSError:
        # Missing is the ordinary case: no proxy on this machine. Unreadable is a
        # misconfiguration, and both mean the same thing here -- there is no front door,
        # so nothing can prove it is one.
        return None
    return content or None


def current_secret() -> Optional[str]:
    """The secret the front door must present, or ``None`` when there is no front door.

    ``None`` is a real answer and the safe one: :func:`agentjobs.principals.is_front_door`
    reads it as "nothing is the front door", so an identity header is ignored rather
    than believed. Failing closed on *attribution* while staying open on *function* is
    the intended shape -- a machine with no proxy keeps working, and every caller on it
    is the owner it actually is.
    """
    injected = os.environ.get(SECRET_ENV, "").strip()
    if injected:
        return injected
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] >= _FILE_CACHE_SECONDS:
        _cache = (now, _read_file())
    return _cache[1]
