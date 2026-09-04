"""Every route that writes, and the capability it needs. Enforced in one dependency.

:mod:`agentjobs.capabilities` says what each kind of principal may do.
This module says which of those a given request is asking for, and refuses it when the
answer is no.

**The table is keyed by the endpoint function, not by the path**, and that is not a
stylistic choice. Every API router in this application is mounted twice -- unscoped at
``/api`` and again at ``/api/projects/{project_id}`` -- so a path-keyed table would need
two entries per route and could silently cover only one of them. There is exactly one
endpoint function behind both mounts.

**Completeness is enforced rather than promised.** ``tests/test_authorization.py`` walks
``app.routes``, filters the mutating methods, and fails when an endpoint is not named in
:data:`ROUTE_CAPABILITIES`. A route added without a decision about who may call it turns
the suite red; it does not ship unchecked. That is the shape of this task's constraint --
"no route may be left un-checked by oversight" -- expressed as something a machine
verifies rather than as an assurance in prose.

**Reads are deliberately absent**, with two exceptions. What the API exposes to a reader
is task-333's question, and answering it here would smuggle a second change into this
one. The exceptions are the run and finish output routes, which are named because ac-5
asks for them identity-gated rather than denied: a run may read its own transcript, and
a person may read any.

**Where the check happens.** One dependency, installed application-wide in
:mod:`agentjobs.api.main`, so it runs for every ``APIRoute`` including any added later.
It is a dependency rather than middleware because middleware runs *before* routing and
therefore knows neither the matched endpoint nor the path parameters; by the time
dependencies are solved, Starlette has put both on the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import Request, status

from agentjobs.capabilities import (
    ACTOR_MISMATCH,
    CAPABILITY_DENIED,
    Capability,
    Denial,
    IDENTITY_UNRESOLVED,
    WRONG_RUN,
    WRONG_TASK,
    actor_disagreement,
    authorize,
)
from agentjobs.principals import Resolution

from .dependencies import get_principal_resolution


@dataclass(frozen=True)
class RouteRule:
    """What one endpoint needs, and what it addresses.

    ``task_param`` and ``run_param`` name the path parameter carrying the thing a run's
    capability is scoped to. They are names rather than booleans because the endpoint
    that reads a *finish*'s output is scoped by ``task_id`` while a run's is scoped by
    ``run_id``, and a boolean could not say which.
    """

    capability: Capability
    task_param: Optional[str] = None
    run_param: Optional[str] = None


_TASK = "task_id"
_RUN = "run_id"


ROUTE_CAPABILITIES: Dict[str, RouteRule] = {
    # ----- the task record itself -------------------------------------------------
    "create_task": RouteRule(Capability.TASK_CREATE),
    "update_task": RouteRule(Capability.TASK_EDIT, task_param=_TASK),
    "archive_task": RouteRule(Capability.TASK_EDIT, task_param=_TASK),
    "mark_deliverable": RouteRule(Capability.TASK_EDIT, task_param=_TASK),
    # ----- the workflow verbs -----------------------------------------------------
    "promote_task": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "claim_task": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "handoff_task": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "release_task": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "close_task": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "append_log_entry": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    "post_progress_update": RouteRule(Capability.TASK_VERB, task_param=_TASK),
    # ----- where a task stands ----------------------------------------------------
    "queue_move_task": RouteRule(Capability.TASK_QUEUE, task_param=_TASK),
    "queue_keep_task": RouteRule(Capability.TASK_QUEUE, task_param=_TASK),
    "reprioritize_task": RouteRule(Capability.TASK_QUEUE, task_param=_TASK),
    # ----- the human review loop --------------------------------------------------
    #
    # No `task_param`, and it would change nothing if there were one: no run holds
    # TASK_REVIEW at all, so the scope check is never reached. Left off so the table
    # does not imply a scoped grant that does not exist.
    "approve_task": RouteRule(Capability.TASK_REVIEW),
    "request_changes": RouteRule(Capability.TASK_REVIEW),
    "answer_task": RouteRule(Capability.TASK_REVIEW),
    "redirect_task": RouteRule(Capability.TASK_REVIEW),
    "hold_task": RouteRule(Capability.TASK_REVIEW),
    "resume_task": RouteRule(Capability.TASK_REVIEW),
    "reject_task": RouteRule(Capability.TASK_REVIEW),
    # ----- spending money ---------------------------------------------------------
    "dispatch_task_endpoint": RouteRule(Capability.DISPATCH),
    "run_playbook_endpoint": RouteRule(Capability.DISPATCH),
    "cancel_dispatch_run": RouteRule(Capability.DISPATCH),
    "enable_dispatch": RouteRule(Capability.DISPATCH_ADMIN),
    "disable_dispatch": RouteRule(Capability.DISPATCH_ADMIN),
    # ----- machine-level administration -------------------------------------------
    "register_project": RouteRule(Capability.PROJECT_ADMIN),
    "initialize_and_register_project": RouteRule(Capability.PROJECT_ADMIN),
    "inspect_project_path": RouteRule(Capability.PROJECT_ADMIN),
    "repair_queue": RouteRule(Capability.QUEUE_ADMIN),
    "compact_queue": RouteRule(Capability.QUEUE_ADMIN),
    "create_webhook": RouteRule(Capability.WEBHOOK_ADMIN),
    "delete_webhook": RouteRule(Capability.WEBHOOK_ADMIN),
    "test_webhook": RouteRule(Capability.WEBHOOK_ADMIN),
    # ----- the reads that are here on purpose --------------------------------------
    #
    # task-332 named the first two, for its ac-5: a run may read its own output and no
    # other's. task-333 added the last two, which are the same file read two other ways
    # -- the tail is the pty capture and the transcript is the JSONL beside it -- and
    # were the only run-output routes with no principal check at all. Leaving them out
    # would have made "a run may read its own transcript and no other's" true of the
    # route nobody polls and false of the two the page actually polls.
    "read_dispatch_run_output": RouteRule(Capability.RUN_OUTPUT, run_param=_RUN),
    "read_task_finish_output": RouteRule(Capability.RUN_OUTPUT, task_param=_TASK),
    "read_dispatch_run_tail": RouteRule(Capability.RUN_OUTPUT, run_param=_RUN),
    "read_dispatch_run_transcript": RouteRule(Capability.RUN_OUTPUT, run_param=_RUN),
}
"""Every mutating endpoint, plus the four output reads. Nothing else is checked here."""


_STATUS: Dict[str, int] = {
    CAPABILITY_DENIED: status.HTTP_403_FORBIDDEN,
    WRONG_TASK: status.HTTP_403_FORBIDDEN,
    WRONG_RUN: status.HTTP_403_FORBIDDEN,
    ACTOR_MISMATCH: status.HTTP_403_FORBIDDEN,
    IDENTITY_UNRESOLVED: status.HTTP_400_BAD_REQUEST,
}
"""HTTP status per denial code.

403 for "you are not allowed to do this", which is every denial that turns on who is
asking. 400 for :data:`~agentjobs.capabilities.IDENTITY_UNRESOLVED`, which is not a
refusal of the caller at all -- it says the machine cannot work out who a legitimate
caller is, and the fix is a config file rather than a different request. Anything not
listed -- the resolution problems out of :mod:`agentjobs.principals` -- is 403: a request
that resolved to nobody is refused, not diagnosed.
"""


def denial_status(code: str) -> int:
    """The HTTP status one denial code answers with."""
    return _STATUS.get(code, status.HTTP_403_FORBIDDEN)


class Forbidden(Exception):
    """A capability or identity refusal, rendered by the handler in ``api.main``.

    Its own exception rather than ``HTTPException`` so the response can carry the denial
    code as a field. A caller that has to parse a sentence to tell "your credential is
    for another run" from "you may not do this at all" will eventually parse it wrong,
    and the two need different responses from whoever hit them.
    """

    def __init__(self, denial: Denial) -> None:
        super().__init__(denial.detail)
        self.denial = denial

    @property
    def status_code(self) -> int:
        return denial_status(self.denial.code)

    def body(self) -> Dict[str, Any]:
        """The response payload: the code to branch on, the sentence to read."""
        return {"code": self.denial.code, "detail": self.denial.detail}


def rule_for(endpoint: Any) -> Optional[RouteRule]:
    """The rule for a matched endpoint, or ``None`` when the table does not name it."""
    name = getattr(endpoint, "__name__", None)
    if not name:
        return None
    return ROUTE_CAPABILITIES.get(name)


async def enforce_capability(request: Request) -> None:
    """Refuse a request whose principal does not hold what the route needs.

    Installed as an application-wide dependency, so it runs for every ``APIRoute``
    without any route opting in. An endpoint the table does not name is not checked here
    -- which is every read except the two output routes -- and
    ``tests/test_authorization.py`` is what stops that becoming an accident.
    """
    rule = rule_for(request.scope.get("endpoint"))
    if rule is None:
        return
    params = request.path_params
    denial = authorize(
        get_principal_resolution(request),
        rule.capability,
        task_id=_param(params, rule.task_param),
        run_id=_param(params, rule.run_param),
    )
    if denial is not None:
        raise Forbidden(denial)


def _param(params: Dict[str, Any], name: Optional[str]) -> Optional[str]:
    """One path parameter as a string, or ``None`` when the rule names none."""
    if name is None:
        return None
    value = params.get(name)
    return None if value is None else str(value)


def assert_actor_agrees(
    request: Request,
    config: Dict[str, Any],
    claimed: str,
    *,
    field: str = "actor",
    require_human: bool = False,
) -> None:
    """Refuse a body actor that disagrees with the principal this request resolved to.

    The HTTP adapter for :func:`agentjobs.capabilities.actor_disagreement`, and
    deliberately the whole of it -- the rule itself takes a config and a resolution and
    can be exercised without a transport.

    Called from the two places every submitted actor already passes through
    (``acting_actor`` and ``acting_user``) plus the handful of routes that take a name in
    a differently-spelled field: the dispatch and playbook ``user``, and the queue
    maintenance ``actor``.
    """
    denial = actor_disagreement(
        config,
        _resolution(request),
        claimed,
        field=field,
        require_human=require_human,
    )
    if denial is not None:
        raise Forbidden(denial)


def _resolution(request: Optional[Request]) -> Resolution:
    """This request's principal resolution, or an empty one when there is no request.

    ``None`` is reachable: a few helpers are called from code paths with no request in
    hand, and an empty resolution refuses rather than waving through -- the caller that
    has no request also has nothing to check.
    """
    if request is None:
        return Resolution()
    return get_principal_resolution(request)
