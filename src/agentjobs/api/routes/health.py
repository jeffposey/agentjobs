"""Health, version and identity endpoints for AgentJobs API."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from agentjobs.__version__ import __version__
from agentjobs.environment import describe_source, source_identity
from agentjobs.models_v2 import SCHEMA_VERSION
from agentjobs.storage import yaml_loader_name

from ..contract import live_contract_digest
from ..dependencies import get_principal_resolution
from ..spa import bundle_id, bundle_is_present

router = APIRouter(prefix="/api", tags=["system"])


class VersionResponse(BaseModel):
    """What a client needs to decide whether it can talk to this service.

    Two independent numbers. ``version`` is the installed AgentJobs distribution and
    governs the shape of the REST surface; ``schema_version`` is the task-record
    schema and governs the shape of the documents that surface returns. A client can
    match one and not the other, so neither is derivable from the other.
    """

    version: str = Field(description="Installed AgentJobs package version.")
    schema_version: int = Field(description="Task record schema version served.")
    yaml_loader: str = Field(
        description=(
            "Which YAML parser reads task files. The pure-Python fallback is about "
            "thirteen times slower than libyaml and is the usual explanation for a "
            "sluggish install, so it is reported rather than left to be guessed at."
        )
    )
    source_root: str = Field(
        description=(
            "Directory this process imported its own code from. Startup refuses when "
            "that is the wrong checkout, but the answer is reported here too: on a "
            "machine with several worktrees it is the difference between a stale "
            "server and a wrongly-installed one, and guessing costs a forensic session."
        )
    )
    source_commit: Optional[str] = Field(
        default=None,
        description=(
            "Git commit this process's source was at when the process started, or null "
            "when the source is not a checkout. Captured at startup and never "
            "recomputed, so it describes the code in memory rather than the files on "
            "disk -- which is what makes it evidence that a merge is actually live "
            "rather than merely committed."
        ),
    )
    started_at: str = Field(
        description=(
            "When this process fixed its identity, in UTC. Independent of the commit: "
            "a process that started before a merge cannot be serving it, whatever any "
            "file on disk now says."
        )
    )
    api_digest: str = Field(
        description=(
            "SHA-256 of the OpenAPI document this process serves. The generated "
            "TypeScript client is built from that same document, so a bundle carries "
            "the digest it was generated against and can tell, in one comparison, "
            "whether the server answering it is the one it was built for. Neither "
            "`version` nor `schema_version` can: in the incident this was added for "
            "both matched exactly while a response field had been added underneath a "
            "running process. See agentjobs.api.contract."
        )
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description=(
            "Which build of the React app this process is serving from disk, or null "
            "when no bundle is present or it was built before this field existed. "
            "Derived from the built asset bytes, so unlike `api_digest` it also moves "
            "for the many rebuilds that change no API route -- which is what lets an "
            "open tab notice it is running the previous build and offer a reload."
        ),
    )
    frontend_bundle: Literal["present", "missing"] = Field(
        description=(
            "Whether this process can serve the React app at /app/. The bundle is "
            "gitignored and no install step builds it, so a clone that has never run "
            "`npm run build` answers every REST call correctly and 404s the one URL a "
            "new user is told to open. Reported here so `agentjobs open` can say so "
            "before opening a browser rather than after."
        )
    )


@router.get("/health")
async def api_health_check() -> dict[str, str]:
    """Simple health check endpoint for API consumers."""
    return {"status": "ok"}


@router.get("/version", response_model=VersionResponse)
async def api_version(request: Request) -> VersionResponse:
    """Report the versions a client must match before it starts issuing calls.

    Added for the MCP server's startup probe, which refuses to serve tools against a
    service it cannot understand. The version is already in ``/openapi.json``, but
    reading it there makes every client parse a large document to learn two fields.

    The React app polls it for a second reason: to notice that the process answering
    it is not the one its own bundle was built against, and to say so with the remedy
    rather than crashing somewhere unrelated when a response is not the shape its
    generated types promise.
    """
    identity = source_identity()
    return VersionResponse(
        version=__version__,
        schema_version=SCHEMA_VERSION,
        yaml_loader=yaml_loader_name(),
        source_root=describe_source(),
        source_commit=identity.commit,
        started_at=identity.started_at,
        api_digest=live_contract_digest(request.app),
        bundle_id=bundle_id(),
        frontend_bundle="present" if bundle_is_present() else "missing",
    )


class WhoAmIResponse(BaseModel):
    """The principal this request resolved to, or the reason none did.

    The observable half of task-329's resolution and task-331's credential: without a
    route that says what the application concluded, "a dispatched run identifies as its
    run" is only assertable against a synthesised header in a unit test. Here a real
    dispatched agent can ask, over the same transport it makes every other call on, and
    get the answer the middleware actually reached.

    It reports identity; it enforces nothing. Every route answers exactly as it did
    before, including for a caller this resolves nothing for -- that is task-332's
    question, and answering it here would smuggle enforcement into a change meant to
    establish identity.

    **It never echoes the credential.** A caller learns which run it is, never the token
    that proved it. There is nothing here a caller did not already present.
    """

    kind: Optional[str] = Field(
        default=None,
        description="tailnet, owner, or run. Null when nothing resolved.",
    )
    source: Optional[str] = Field(
        default=None,
        description=(
            "How the identity was established: run_credential, proven_header, or "
            "loopback. Kept separate from the kind because an audit trail that records "
            "only what a caller is cannot answer on what evidence."
        ),
    )
    login: Optional[str] = Field(
        default=None,
        description="The raw identity the front door proved. Set for kind tailnet only.",
    )
    actor_id: Optional[str] = Field(
        default=None,
        description=(
            "The configured actor this principal maps to. Null until task-330 owns that "
            "mapping, and null forever for a run -- a run is not a human and must never "
            "be attributed as one."
        ),
    )
    run_id: Optional[str] = Field(
        default=None, description="The dispatched run asking. Set for kind run only."
    )
    task_id: Optional[str] = Field(
        default=None, description="The task that run is working. Set for kind run only."
    )
    problem: Optional[str] = Field(
        default=None,
        description=(
            "Why nothing resolved: no_proven_identity, unverified_run_credential, or "
            "expired_run_credential. An expired credential is reported as its own "
            "problem rather than resolving the owner -- falling back to a more capable "
            "principal on expiry would invert the control."
        ),
    )
    detail: str = Field(
        default="",
        description="A sentence a person can act on when nothing resolved.",
    )
    describe: str = Field(
        description="One line naming the kind and the evidence, as a log would record it."
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def api_whoami(request: Request) -> WhoAmIResponse:
    """Report who this request resolved as. Reports only; refuses nothing."""
    resolution = get_principal_resolution(request)
    principal = resolution.principal
    return WhoAmIResponse(
        kind=principal.kind.value if principal else None,
        source=principal.source.value if principal else None,
        login=principal.login if principal else None,
        actor_id=principal.actor_id if principal else None,
        run_id=principal.run_id if principal else None,
        task_id=principal.task_id if principal else None,
        problem=resolution.problem.value if resolution.problem else None,
        detail=resolution.detail,
        describe=resolution.describe(),
    )
