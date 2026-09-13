"""The execution envelope: what one accepted dispatch was granted, frozen (task-375).

A dispatch is admitted with an envelope -- runner, group, posture, push, the exact policy
clause the agent is told -- and that envelope is recorded in the execution journal before
any worker exists. This module builds it, and decides when a later dispatch **continues**
an earlier grant rather than making a new one.

**Why it has to be frozen.** Before this, every dispatch re-read today's configuration.
A retry or resume therefore ran whatever the project default had become: task-410's
big-dawg run on ``claude-fable-5-1`` came back on ``claude-opus-5`` when its feedback was
delivered, and nothing on the record said the model had changed. The same re-reading let
a record say ``autonomous`` about a resumed session that had only ever been told ``auto``
(task-358's child task-273).

**Continuation versus new grant.** The line is who acted, not which code path ran:

- A dispatch the *machine* starts on a human's handback -- trigger ``auto``, naming no
  runner, group, posture, authoriser or epic -- continues the newest execution
  for that task. It reuses the recorded runner, group and posture, and says so with
  source ``history``.
- A person's click or command, and an epic child started on its parent's authorisation,
  are new grants and resolve against configuration as it stands now. Treating them as
  history would let a fresh click inherit an old epic's ``autonomous`` without anyone
  choosing it again.

**Frozen is not irrevocable.** A continuation still passes every gate a new dispatch
passes, observed now: the kill switch, disabled dispatch, a hold, the ceiling. A
revocation wins and a widening grants nothing -- see ``config.resolve_posture`` and
``config.resolve_recorded_runner``. A Stop recorded against the execution being continued
refuses the continuation outright: dispatching again is how a person grants a new one.

**What is never in it.** Secret values. A runner's ``env`` is where its secrets live, so
the envelope records only the variable *names*; the run's API credential is minted per
run and never copied from one run to the next (``dispatch.credentials``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from agentjobs.__version__ import __version__
from agentjobs.dispatch.config import (
    DispatchError,
    DispatchResolution,
    Posture,
    ResolvedPosture,
)
from agentjobs.execution.reducer import WORKFLOW_VERSION
from agentjobs.execution.store import PROVENANCE_NATIVE, Execution, ExecutionStore
from agentjobs.models_v2 import DispatchTrigger

if TYPE_CHECKING:  # pragma: no cover - guards imports this module
    from agentjobs.dispatch.guards import DispatchRequest

ENVELOPE_VERSION = 1
"""Bumped when a field's meaning changes. An envelope with no version predates this module
(task-264 recorded a partial one); it is read for the fields it has and never completed
from today's defaults."""

GRANT = "grant"
HISTORY = "history"


class GrantStoppedError(DispatchError):
    """The execution a continuation would carry on was stopped by a person.

    A Stop cancels intent (design section 9a). Continuing its envelope automatically --
    because a handback arrived afterwards -- would restart the work the Stop ended on an
    authority nobody renewed. Dispatching the task again is a new grant and is not refused.
    """

    reason = "grant_stopped"


@dataclass(frozen=True)
class History:
    """The envelope a continuation reuses, and where it came from."""

    execution_id: str
    """The execution being continued -- the newest one for this project/task."""
    root_execution_id: str
    """The execution whose grant this chain of continuations started from."""
    runner: str
    group: Optional[str]
    posture: Posture
    push: Optional[bool]
    """``None`` for an envelope recorded before push was frozen."""
    stop: Optional[Dict[str, Any]]
    """The ``stop_requested`` payload recorded against that execution, if any."""


def is_continuation(request: "DispatchRequest") -> bool:
    """Whether this dispatch carries on an earlier grant instead of making a new one.

    The playbook pointer is deliberately not consulted: no gate reads one (playbooks
    design section 6.3), and a playbook run is a person's ``manual`` dispatch anyway.
    """
    return (
        request.trigger is DispatchTrigger.AUTO
        and not request.runner
        and not request.group
        and request.posture is None
        and not (request.authorized_by or "").strip()
        and not request.on_behalf_of_parent
    )


def continuation_history(
    store: ExecutionStore,
    project_id: str,
    task_id: str,
    *,
    execution_id: Optional[str] = None,
) -> Optional[History]:
    """The envelope a continuation reuses, when there is one to continue.

    ``execution_id`` names the execution outright -- a retry *within* an execution the
    durable controller is recovering (task-416) reads that execution's frozen envelope
    and no other, even if something newer exists. Without it, the newest execution for
    this project/task, as task-375 built it for a handback.

    ``None`` when there is nothing to continue: no execution at all, a legacy import, or
    an envelope that never recorded a runner and a valid posture. Those resolve as a new
    dispatch did before this module; a missing field is never filled in from today's
    configuration, because that is exactly the silent substitution this exists to stop.
    """
    if execution_id is not None:
        execution = store.execution(execution_id)
        if execution is None or execution.terminal or (execution.project_id, execution.task_id) != (
            project_id,
            task_id,
        ):
            raise DispatchError(
                f"execution {execution_id} is not an open execution of {project_id}/{task_id}, "
                "so nothing can be retried within it"
            )
    else:
        execution = store.latest_execution(project_id, task_id)
    if execution is None or execution.provenance != PROVENANCE_NATIVE:
        return None
    envelope = execution.envelope
    runner = envelope.get("runner")
    try:
        posture = Posture(str(envelope.get("posture")))
    except ValueError:
        return None
    if not isinstance(runner, str) or not runner:
        return None
    group = envelope.get("group")
    push = envelope.get("push")
    recorded_grant = envelope.get("grant")
    grant: Mapping[str, Any] = recorded_grant if isinstance(recorded_grant, Mapping) else {}
    return History(
        execution_id=execution.execution_id,
        root_execution_id=str(grant.get("root_execution_id") or execution.execution_id),
        runner=runner,
        group=group if isinstance(group, str) and group else None,
        posture=posture,
        push=push if isinstance(push, bool) else None,
        stop=_stop_request(store, execution),
    )


def _stop_request(store: ExecutionStore, execution: Execution) -> Optional[Dict[str, Any]]:
    for event in store.events(execution.execution_id):
        if event.kind == "stop_requested":
            return dict(event.payload)
    return None


def assert_not_stopped(history: History, task_id: str) -> None:
    """Refuse to continue an execution a person stopped."""
    if history.stop is None:
        return
    who = history.stop.get("requester") or "someone"
    where = history.stop.get("source") or "an unrecorded surface"
    raise GrantStoppedError(
        f"{task_id}'s last execution ({history.execution_id}) was stopped by {who} from "
        f"{where}, so it is not continued automatically. Dispatch the task again to grant "
        "a new run."
    )


def settings_digest(resolution: DispatchResolution) -> str:
    """A stable digest of the project's dispatch settings, for spotting a change later."""
    payload = json.dumps(asdict(resolution.settings), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_envelope(
    resolution: DispatchResolution,
    posture: ResolvedPosture,
    request: "DispatchRequest",
    *,
    push: bool,
    policy_clause: str,
    history: Optional[History],
    retry_policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The non-secret, versioned envelope an execution is accepted with.

    Values, not only a digest: a later reader -- or the continuation that reuses it --
    has to be able to reproduce what was granted, and a hash cannot be read back. The
    runner's ``env`` contributes its variable names and never their values.
    """
    runner = resolution.runner
    selection = resolution.selection
    return {
        "envelope_version": ENVELOPE_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "adapter_version": __version__,
        "runner": runner.name,
        "driver": runner.driver.value,
        "mode": runner.mode.value,
        "argv_template": list(runner.argv),
        "env_keys": sorted(runner.env),
        "group": selection.group if selection is not None else None,
        "selection_source": selection.source.value if selection is not None else None,
        "candidates": (
            [
                {
                    "runner": candidate.runner,
                    "eligible": candidate.eligible,
                    "selected": candidate.selected,
                    "skipped_because": (
                        candidate.skipped_because.value if candidate.skipped_because else None
                    ),
                }
                for candidate in selection.candidates
            ]
            if selection is not None
            else []
        ),
        "posture": posture.posture.value,
        **posture.as_data(),
        "push": push,
        "merge_policy": posture.posture.merge_policy.value,
        "policy_clause": policy_clause,
        "settings_digest": settings_digest(resolution),
        "trigger": request.trigger.value,
        "requested_runner": request.runner,
        "requested_group": request.group,
        "grant": (
            {
                "kind": HISTORY,
                "continues_execution_id": history.execution_id,
                "root_execution_id": history.root_execution_id,
            }
            if history is not None
            else {"kind": GRANT}
        ),
        # Frozen with everything else (task-416): an execution's recovery bound is part of
        # its grant, so a later change to the default cannot give a running execution
        # more attempts than it was accepted with. Absent means no automatic retry.
        **({"retry_policy": dict(retry_policy)} if retry_policy is not None else {}),
    }


__all__ = [
    "ENVELOPE_VERSION",
    "GrantStoppedError",
    "History",
    "assert_not_stopped",
    "build_envelope",
    "continuation_history",
    "is_continuation",
    "settings_digest",
]
