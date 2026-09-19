"""The owner's devices, and the key they subscribe against (task-423).

Four routes and one subject: which phones and tablets AgentJobs may wake when work
stops on this person. They are project-scoped because the attention episode is, and
mounted like every other project router.

**All four need ``push.manage``, including the read.** Registering a subscription is
choosing whose lock screen AgentJobs writes on, which is not a thing an agent gets to
decide; and the read is where the machine's VAPID keypair comes into existence, so it
is not the pure read its verb suggests. See
:mod:`agentjobs.api.authorization` for where that is written down.

**No endpoint here is on the path of a handoff.** Delivery is the watcher's job and
happens afterwards, which is what makes it safe for a push service to be slow, broken,
or unreachable: the worst it can do is leave a status on a device row.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from agentjobs.attention import read_episode
from agentjobs.projects import Project
from agentjobs.push import (
    DETAIL_MODES,
    Subscription,
    load,
    load_or_create,
    new_subscription_id,
    remove,
    send_test,
    upsert,
)
from agentjobs.push.keys import contact
from agentjobs.push.watcher import is_watching, poll_interval

from ..dependencies import get_project
from ..models import (
    PushDeviceView,
    PushSendResult,
    PushStatusResponse,
    PushSubscribeRequest,
    PushTestRequest,
    PushTestResponse,
    PushUnsubscribeRequest,
)

router = APIRouter(tags=["push"])


def _status(project: Project) -> PushStatusResponse:
    """The panel's whole state: the key to subscribe with, and what is registered.

    ``load_or_create`` is called on every read rather than only when a device
    subscribes, which is what makes the feature have no setup step: the first person to
    open the notifications panel generates the machine's keypair, and everybody after
    that gets the same one.
    """
    key = load_or_create()
    devices = [PushDeviceView(**row.view()) for row in load(project.id)]
    return PushStatusResponse(
        vapid_public_key=key.public_key,
        contact=contact(),
        watching=is_watching(),
        poll_seconds=poll_interval(),
        devices=devices,
    )


@router.get("/push", response_model=PushStatusResponse)
async def get_push_status(
    project: Project = Depends(get_project),
) -> PushStatusResponse:
    """The application-server key, and every device registered for this project."""
    return _status(project)


@router.post("/push/subscribe", response_model=PushStatusResponse)
async def subscribe_push_device(
    payload: PushSubscribeRequest,
    project: Project = Depends(get_project),
) -> PushStatusResponse:
    """Register a device, or refresh one that already exists.

    **A device registered mid-episode is not immediately pushed.** The new row inherits
    nothing, so ``last_episode_id`` is seeded with whatever episode is currently open:
    a person who opts in from a page that is already showing "3 waiting" has seen the
    three, and interrupting them about it a second later would be the alert fatigue the
    episode model exists to prevent. The *next* episode wakes them, which is the thing
    they subscribed for; ``POST /push/test`` is how they confirm it works before then.

    Re-subscribing an endpoint that is already registered keeps its id and its episode
    id -- see :func:`agentjobs.push.subscriptions.upsert`.
    """
    if payload.detail not in DETAIL_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"detail must be one of {', '.join(DETAIL_MODES)}",
        )
    if not payload.endpoint.lower().startswith("https://"):
        # Every push service is HTTPS, and an endpoint that is not is either a
        # misconfiguration or somebody pointing AgentJobs at a listener of their own.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a push endpoint must be an https URL",
        )

    episode = read_episode(project.id)
    upsert(
        project.id,
        Subscription(
            id=new_subscription_id(),
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            label=payload.label.strip()[:80],
            detail=payload.detail,
            last_episode_id=episode.id if episode else None,
        ),
    )
    return _status(project)


@router.post("/push/unsubscribe", response_model=PushStatusResponse)
async def unsubscribe_push_device(
    payload: PushUnsubscribeRequest,
    project: Project = Depends(get_project),
) -> PushStatusResponse:
    """Forget a device, by id from the page or by endpoint from a service worker.

    Idempotent, and silent about whether anything matched. A device is routinely
    removed from both ends at once -- the button here and the browser's own permission
    reset -- and a 404 for the second of those would be an error message about a race.
    """
    if not payload.subscription_id and not payload.endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name the device by subscription_id or by endpoint",
        )
    remove(
        project.id,
        subscription_id=payload.subscription_id,
        endpoint=payload.endpoint,
    )
    return _status(project)


@router.post("/push/test", response_model=PushTestResponse)
async def send_test_push(
    payload: PushTestRequest,
    project: Project = Depends(get_project),
) -> PushTestResponse:
    """Prove delivery to one device, or to all of them.

    The answer to "did that actually work", which nothing else gives: a device that
    opted in while nothing was waiting would otherwise find out whether push reaches it
    at the moment it matters least. It does not touch the episode -- a test must not be
    able to consume the interruption a real episode is owed.
    """
    devices = load(project.id)
    if payload.subscription_id:
        devices = tuple(row for row in devices if row.id == payload.subscription_id)
        if not devices:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such device")
    results: List[PushSendResult] = []
    for device in devices:
        outcome = send_test(project.id, device)
        results.append(
            PushSendResult(
                subscription_id=device.id,
                outcome=outcome.outcome,
                status=outcome.status,
                error=outcome.error,
            )
        )
    return PushTestResponse(results=results)


__all__ = ["router"]
