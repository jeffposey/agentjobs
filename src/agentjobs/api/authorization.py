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
    # The one verb that reaches into the append-only log. TASK_EDIT rather than a
    # capability of its own: it changes the text of a record, which is what editing
    # is, and a per-verb capability here would be the role system task-066 refused.
    "redact_task_region": RouteRule(Capability.TASK_EDIT, task_param=_TASK),
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
    # ----- saying you have seen it ------------------------------------------------
    #
    # Names no task, because an episode is not about one: it is the whole waiting set
    # as one thing, which is the point of the episode model.
    "acknowledge_attention": RouteRule(Capability.ATTENTION_ACK),
    # ----- the owner's own devices ------------------------------------------------
    #
    # The read is here on purpose, unlike every other read in this application. Two
    # reasons, and either would do: it answers with the labels of the devices a person
    # carries, and calling it is what generates the machine's VAPID keypair, so it is
    # not the pure read its verb suggests.
    "get_push_status": RouteRule(Capability.PUSH_MANAGE),
    "subscribe_push_device": RouteRule(Capability.PUSH_MANAGE),
    "unsubscribe_push_device": RouteRule(Capability.PUSH_MANAGE),
    "send_test_push": RouteRule(Capability.PUSH_MANAGE),
    # ----- spending money ---------------------------------------------------------
    #
    # `draft_task_spec` names no task because there is no task yet -- it fills a form
    # somebody is standing in. Its sibling `get_model_status` is deliberately absent:
    # it is a read that answers a boolean about this machine's configuration, and
    # gating it would leave a run unable to discover that it may not draft.
    "draft_task_spec": RouteRule(Capability.MODEL_DRAFT),
    "dispatch_task_endpoint": RouteRule(Capability.DISPATCH),
    "run_playbook_endpoint": RouteRule(Capability.DISPATCH),
    "cancel_dispatch_run": RouteRule(Capability.DISPATCH),
    # Relaying a human's authorisation writes a log entry and starts nothing, so on
    # shape alone it belongs beside `append_log_entry` under `task.verb` -- which every
    # run holds, which is exactly why it is here instead (task-506). The entry it writes
    # is the one row `assert_human_clocked` accepts from a writer who is not a human, so
    # a run able to write one could authorise its own successor. It sits under
    # `dispatch.*` because that is the door it opens, not because it opens it itself.
    #
    # `task_param` for the ordinary reason: it is a write to one named task. It changes
    # nothing today -- `OWN_TASK_ONLY` is empty and no run holds this at all -- and is
    # named so that re-scoping the capability one day scopes this route with it.
    "relay_authorization": RouteRule(Capability.DISPATCH_RELAY, task_param=_TASK),
    "enable_dispatch": RouteRule(Capability.DISPATCH_ADMIN),
    "disable_dispatch": RouteRule(Capability.DISPATCH_ADMIN),
    # Arming the pull mode is the strongest thing on this table by consequence
    # (task-462): it is a standing authority to start runs without a further click. It
    # sits under `DISPATCH_ADMIN` rather than `DISPATCH` because it is not one purchase,
    # it is permission for the machine to keep making them -- and because a run holds
    # neither, the property that matters is already true either way: **an agent cannot
    # arm the machine to keep starting agents.** Disarming shares the capability for the
    # ordinary reason a kill switch shares one with its switch: everyone who may turn it
    # on may turn it off.
    "arm_pull_mode": RouteRule(Capability.DISPATCH_ADMIN),
    "disarm_pull_mode": RouteRule(Capability.DISPATCH_ADMIN),
    # Switching the idle-session sweep on lets it stop the owner's own sessions (task-447).
    "update_idle_session_settings": RouteRule(Capability.DISPATCH_ADMIN),
    # ----- the history index (task-472) --------------------------------------------
    #
    # No `task_param`: a gate run may name no task at all, and the finish names its task
    # in the body rather than the path. Neither is scoped to a run's own task anyway,
    # since `OWN_TASK_ONLY` is empty.
    "record_finish_history": RouteRule(Capability.HISTORY_RECORD),
    "record_gate_history": RouteRule(Capability.HISTORY_RECORD),
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


def assert_holds(request: Request, capability: Capability) -> None:
    """Refuse a request whose principal does not hold ``capability``.

    The same check :func:`enforce_capability` runs, asked about a *field* rather than a
    route. One endpoint, two capabilities: ``POST .../dispatch`` needs ``dispatch.start``
    from everyone, and ``dispatch.over_ceiling`` as well from a body that asks to exceed
    the machine's ceiling (task-461).

    **The route rule fires first, so a run reaching the second check is currently
    impossible** -- no run holds ``dispatch.start``. That is the argument for having it
    rather than against: the overage is granted on a different question from the one
    ``dispatch.start`` answers, and a day when a run is allowed to spend money is not a
    day when it should also be allowed to spend it past the ceiling. Written here so
    that widening the first grants nothing of the second.
    """
    denial = authorize(_resolution(request), capability)
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
