"""Startup probe and version policy for the MCP server.

The MCP process is a facade over a *separately running* AgentJobs HTTP service. It
must never start or own that service, so the only honest thing it can do when the
service is missing or too old is refuse to start and say why. Discovering the
mismatch here -- once, on stderr, before the client sees a tool list -- is far better
than discovering it as a confusing failure inside somebody's third tool call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..client import TaskClient, TaskClientError

#: Fields the MCP tools read off every project record. Their presence is the proof
#: that a version-skewed service still speaks a surface we can use.
REQUIRED_PROJECT_FIELDS = ("id", "name", "root", "tasks_directory")

_VERSION_PATTERN = re.compile(r"^\s*(\d+)\.(\d+)")


class StartupError(RuntimeError):
    """The MCP server cannot serve tools against the configured service."""


@dataclass(frozen=True)
class ServiceInfo:
    """What the probe learned about the service on the other end."""

    base_url: str
    version: str
    schema_version: int
    project_ids: Tuple[str, ...]


def parse_version(value: str) -> Optional[Tuple[int, int]]:
    """Return ``(major, minor)`` for a semantic version, or None if unparseable."""
    match = _VERSION_PATTERN.match(value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def compatibility_key(value: str) -> Optional[Tuple[int, ...]]:
    """Reduce a version to the part that must match exactly.

    Semver promises compatibility within a major version -- except below 1.0, where
    it promises nothing and the minor is where breaking changes land. AgentJobs is
    still 0.x, so comparing majors alone would call 0.1 and 0.9 compatible and let a
    real break through as a confusing runtime error instead of a startup message.
    """
    parsed = parse_version(value)
    if parsed is None:
        return None
    major, minor = parsed
    return (0, minor) if major == 0 else (major,)


def check_version(*, client_version: str, server_version: str) -> Optional[str]:
    """Return an actionable message when the two versions cannot work together."""
    client_key = compatibility_key(client_version)
    server_key = compatibility_key(server_version)
    if client_key is None or server_key is None:
        return (
            f"Cannot compare AgentJobs versions (client {client_version!r}, "
            f"service {server_version!r}). Expected semantic versions like 1.2.3."
        )
    if client_key != server_key:
        return (
            f"AgentJobs version mismatch: this MCP server is {client_version} but the "
            f"service at the configured URL is {server_version}. Upgrade whichever is "
            "older so both come from the same AgentJobs release."
        )
    return None


def check_schema(*, client_schema: int, server_schema: int) -> Optional[str]:
    """Return an actionable message when task documents would be unreadable."""
    if client_schema != server_schema:
        return (
            f"AgentJobs task schema mismatch: this MCP server understands schema "
            f"v{client_schema} but the service serves v{server_schema}. Run "
            "`agentjobs migrate-schema` or install a matching AgentJobs version."
        )
    return None


def probe_service(
    client: TaskClient,
    *,
    client_version: str,
    client_schema: int,
) -> ServiceInfo:
    """Verify the configured service is reachable, compatible, and usable.

    Raises :class:`StartupError` with a message a human can act on. It never starts a
    service and never falls back to a different URL.
    """
    base_url = client.base_url
    try:
        client.service_health()
    except TaskClientError as exc:
        raise StartupError(_unreachable(base_url, exc)) from exc

    try:
        version_payload = client.service_version()
    except TaskClientError as exc:
        # A 404 here is the one case where the service answered and still cannot be
        # used: it is an AgentJobs old enough not to have /api/version. Reporting
        # that as "not reachable" sends the reader to look for a server that is
        # demonstrably running -- which is exactly what happened the first time this
        # ran against a service left over from before the endpoint existed.
        if exc.status_code == 404:
            raise StartupError(_predates_version_endpoint(base_url)) from exc
        raise StartupError(_unreachable(base_url, exc)) from exc

    try:
        projects = client.list_projects()
    except TaskClientError as exc:
        raise StartupError(_unreachable(base_url, exc)) from exc

    server_version = str(version_payload.get("version", ""))
    raw_schema = version_payload.get("schema_version")
    if not server_version or not isinstance(raw_schema, int):
        raise StartupError(_predates_version_endpoint(base_url))

    for message in (
        check_version(client_version=client_version, server_version=server_version),
        check_schema(client_schema=client_schema, server_schema=raw_schema),
    ):
        if message is not None:
            raise StartupError(message)

    return ServiceInfo(
        base_url=base_url,
        version=server_version,
        schema_version=raw_schema,
        project_ids=tuple(_project_ids(projects, base_url=base_url)),
    )


def _unreachable(base_url: str, exc: TaskClientError) -> str:
    return (
        f"AgentJobs service at {base_url} is not reachable ({exc}). "
        "Start it with `agentjobs serve`, or point AGENTJOBS_URL at the running "
        "service. The MCP server does not start one for you."
    )


def _predates_version_endpoint(base_url: str) -> str:
    return (
        f"AgentJobs service at {base_url} did not report a usable version. It "
        "predates the /api/version endpoint the MCP server requires; upgrade the "
        "service to a matching AgentJobs release and restart it."
    )


def _project_ids(projects: Sequence[Dict[str, Any]], *, base_url: str) -> List[str]:
    """Read project IDs, proving the fields the tools depend on are all present."""
    identifiers: List[str] = []
    for project in projects:
        missing = [name for name in REQUIRED_PROJECT_FIELDS if name not in project]
        if missing:
            raise StartupError(
                f"AgentJobs service at {base_url} returned a project record missing "
                f"{', '.join(missing)}. The MCP server needs those fields to route "
                "tool calls; upgrade the service to a matching AgentJobs release."
            )
        identifiers.append(str(project["id"]))
    return identifiers
