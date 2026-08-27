"""Configuration for the ``agentjobs mcp`` STDIO server.

Two settings, both resolvable from the environment so an MCP client launcher can
configure the process without command-line arguments (`.mcp.json` and most client
configs pass environment more comfortably than argv).

**The base URL has three sources, and the middle one is the point** (task-317). A
client config that hardcodes a port is correct for exactly one machine, and this
repository shipped one that was correct for none: ``plugins/agentjobs/.mcp.json``
named ``:8765``, which is the port this project's own instructions single out as the
one nothing serves here, so the plugin's server refused at startup in every session
and the tools it advertises were never there. Removing the hardcoded value is only
half a fix -- it has to fall through to something that knows.

So: an explicit argument, then ``AGENTJOBS_URL``, then **the address this machine has
declared** -- ``AGENTJOBS_API_BASE``, then ``api_base:`` in
``~/.agentjobs/dispatch.yaml`` -- and only then :data:`DEFAULT_BASE_URL`. The third
source is the same one dispatch already resolves the agent-facing address from
(``agentjobs.dispatch.address``), which is what makes it worth reaching for: a machine
serving on a non-default port states that once, in one file, and both stop being wrong.
:data:`DEFAULT_BASE_URL` stays the last resort because a machine that has declared
nothing and runs ``agentjobs serve`` with no arguments really is on 8765.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from agentjobs.dispatch.address import configured_api_base

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
"""Last resort only, for a machine that has declared no address of its own.

Deliberately the same port as ``agentjobs.dispatch.address.DEFAULT_API_BASE``: both are
"what ``agentjobs serve`` binds when nobody said otherwise", and a client config that
disagrees with the CLI's own default is a worse kind of wrong than a stale default.
"""
DEFAULT_TIMEOUT = 30.0

BASE_URL_ENV = "AGENTJOBS_URL"
TIMEOUT_ENV = "AGENTJOBS_TIMEOUT"

# A tool call that has not returned in five minutes is not going to. The ceiling
# exists so a misconfigured timeout cannot wedge an agent session on a service that
# never answers -- an unbounded client timeout turns a dead server into a hang with
# no diagnostic, which is the worst of the available failure modes.
MAX_TIMEOUT = 300.0


class ConfigError(ValueError):
    """Raised when supplied configuration cannot be used."""


@dataclass(frozen=True)
class McpConfig:
    """Resolved settings for one MCP server process."""

    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def resolve(
        cls,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "McpConfig":
        """Build a config from explicit arguments, then environment, then defaults.

        See the module docstring for why the machine's declared address sits between
        ``AGENTJOBS_URL`` and the default rather than being absent.
        """
        environ = os.environ if env is None else env
        resolved_url = (
            _clean_url(base_url)
            or _clean_url(environ.get(BASE_URL_ENV))
            or _clean_url(configured_api_base(env=environ))
        )
        resolved_timeout = timeout
        if resolved_timeout is None:
            resolved_timeout = _parse_timeout(environ.get(TIMEOUT_ENV))
        return cls(
            base_url=resolved_url or DEFAULT_BASE_URL,
            timeout=_validated_timeout(resolved_timeout),
        )


def _clean_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    trimmed = value.strip().rstrip("/")
    return trimmed or None


def _parse_timeout(value: Optional[str]) -> Optional[float]:
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{TIMEOUT_ENV} must be a number of seconds, got {value!r}.") from exc


def _validated_timeout(value: Optional[float]) -> float:
    if value is None:
        return DEFAULT_TIMEOUT
    if value <= 0:
        raise ConfigError("Timeout must be greater than zero seconds.")
    if value > MAX_TIMEOUT:
        raise ConfigError(f"Timeout must not exceed {MAX_TIMEOUT:.0f} seconds.")
    return value
