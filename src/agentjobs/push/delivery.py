"""What a push says, who is owed one, and what became of the attempt (task-423).

**The policy is not re-decided here.** Whether an interruption is owed is
:func:`agentjobs.attention.owes_notification`, the same function the desktop client's
``shouldNotify`` mirrors, asked against the same episode the badge reads. A phone is
just another client with its own last-notified id; the only thing that differs is where
that id is kept -- ``localStorage`` there, the subscription row here. That is the whole
of "do not invent a second acknowledgment policy": there is one episode, one
acknowledgment, and two places that remember having drawn it.

**The payload is a wake-up signal, and on a phone that is stricter than on a desk.**
The desktop toast names the lead task, because a desktop toast appears on the screen
you are already sitting in front of. A push appears on a lock screen, in a hallway, on
a watch, over somebody's shoulder. So the default says the *number* and nothing else,
and naming the task is an opt-in per device (:data:`~agentjobs.push.subscriptions.DETAIL_TASK`).
The complete ask is in the task record either way; a person who has read a handoff in a
bubble has read it in the one place they cannot act on it.

**Failure is recorded, never raised.** Nothing here is on the path of a handoff -- the
watcher runs out of band, after the fact -- so a push service having a bad hour must
not be able to affect a task write. Each attempt lands on the device row as a status,
an error and a consecutive-failure count, which is what
``GET /api/projects/{id}/push`` shows a person asking "is my phone still getting these".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from ..attention import (
    AttentionState,
    ask_phrase,
    owes_notification,
    reconcile,
    waiting_path,
)
from ..manager import TaskManager
from .keys import VapidKey, authorization_header, load_or_create
from .subscriptions import (
    DETAIL_TASK,
    Subscription,
    load,
    mutate,
)
from .webpush import b64url_decode, encrypt

NOTIFICATION_TAG_PREFIX = "agentjobs-attention-"
"""Shared with ``components/attention/episode.ts``, deliberately.

One tag per project means the OS *replaces* rather than stacks -- and on an installed
desktop PWA, which can receive both the page's own notification and a push, it means
the second one updates the first instead of leaving two entries saying different
numbers.
"""

DEFAULT_TTL_SECONDS = 12 * 3600
"""How long a push service should hold a message for a device that is offline.

Long, because the case this feature exists for is a person who has left, and a wake-up
that expires in five minutes is a wake-up for somebody at their desk. Long is only safe
because the service worker **re-reads the current attention state** when a push
arrives and renders that instead of the payload (``service-worker.js``): a twelve-hour
-old message therefore wakes the device with today's number rather than yesterday's.
"""

TIMEOUT_SECONDS = 10.0

GONE_STATUSES = frozenset({404, 410})
"""The push service saying this endpoint no longer exists. The only thing that deletes
a device row -- see :data:`~agentjobs.push.subscriptions.UNHEALTHY_AFTER`."""

TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

BACKOFF_CAP_SECONDS = 3600
"""Ceiling on the wait between retries of one transient failure."""


@dataclass(frozen=True)
class PushPayload:
    """What the device is sent, before encryption."""

    episode_id: str
    blocking: int
    title: str
    body: str
    url: str
    tag: str

    def encode(self, project_id: str) -> bytes:
        return json.dumps(
            {
                "kind": "attention",
                "project": project_id,
                "episode": self.episode_id,
                "blocking": self.blocking,
                "title": self.title,
                "body": self.body,
                "url": self.url,
                "tag": self.tag,
            },
            separators=(",", ":"),
        ).encode("utf-8")


def notification_tag(project_id: str) -> str:
    return f"{NOTIFICATION_TAG_PREFIX}{project_id}"


def payload_for(state: AttentionState, project_id: str, *, detail: str) -> Optional[PushPayload]:
    """The push for the current episode, or ``None`` when there is nothing to say.

    ``detail`` is the device's own setting rather than a server-wide one, because the
    right answer differs per device: a tablet on a desk at home and a phone on a train
    are the same person with different bystanders.
    """
    episode = state.episode
    if episode is None or state.blocking <= 0:
        return None

    count = state.blocking
    title = "1 task is waiting on you" if count == 1 else f"{count} tasks are waiting on you"
    body = "Open AgentJobs to see what has stopped."
    if state.waiting:
        lead = state.waiting[0]
        # The ask carries in *both* detail modes, because it is not task content: it
        # says what is wanted, never what the work is. "Needs review" is what tells the
        # person whether to go and find a computer, which is the whole job of this line,
        # while telling someone reading over their shoulder nothing about the project.
        # Only the id and title are withheld at ``count``.
        ask = ask_phrase(lead)
        named = f"{lead.id}: {lead.title}".strip() if detail == DETAIL_TASK else ""
        head = f"{ask} — {named}" if ask and named else ask or named
        if head:
            others = count - 1
            body = (
                f"{head} — and {others} other{'' if others == 1 else 's'}."
                if count > 1
                else head
            )

    return PushPayload(
        episode_id=episode.id,
        blocking=count,
        title=title,
        body=body,
        url=waiting_path(project_id, episode),
        tag=notification_tag(project_id),
    )


@dataclass(frozen=True)
class SendResult:
    """What one POST to one endpoint did.

    ``outcome`` rather than the status alone, because three very different things share
    a 4xx: a dead endpoint to forget, a refusal to record, and a rate limit to wait out.
    """

    outcome: str  # "sent" | "gone" | "rejected" | "transient"
    status: Optional[int] = None
    error: Optional[str] = None


def _classify(status: int, text: str) -> SendResult:
    if 200 <= status < 300:
        return SendResult("sent", status)
    if status in GONE_STATUSES:
        return SendResult("gone", status, "the push service no longer knows this device")
    if status in TRANSIENT_STATUSES:
        return SendResult("transient", status, text[:200] or "the push service is busy")
    # 400, 401, 403, 413 and anything unclassified. Refusing rather than retrying is
    # the safe default for an unknown status: a retry loop against a service that has
    # made up its mind is how an application server gets its VAPID key blocked.
    return SendResult("rejected", status, text[:200] or "the push service refused it")


def send(
    subscription: Subscription,
    payload: bytes,
    *,
    key: VapidKey,
    client: httpx.Client,
    ttl: int = DEFAULT_TTL_SECONDS,
    urgency: str = "normal",
) -> SendResult:
    """Encrypt *payload* for one device and POST it. Never raises."""
    try:
        encrypted = encrypt(
            payload,
            receiver_public_key=b64url_decode(subscription.p256dh),
            auth_secret=b64url_decode(subscription.auth),
        )
    except Exception as exc:  # noqa: BLE001 - a malformed row must not stop the round
        # Malformed keys are a property of the row, not of the service, so this is a
        # refusal rather than something to retry: the device has to subscribe again.
        return SendResult("rejected", None, f"could not encrypt for this device: {exc}")

    headers = {
        "Authorization": authorization_header(key, subscription.endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    }
    try:
        response = client.post(subscription.endpoint, content=encrypted.body, headers=headers)
    except httpx.HTTPError as exc:
        return SendResult("transient", None, f"could not reach the push service: {exc}")
    return _classify(response.status_code, response.text or "")


def _retry_due(subscription: Subscription, now: datetime) -> bool:
    """Whether a device whose last attempt failed transiently may be tried again.

    Exponential from two seconds to an hour. The bound on retrying is not a counter:
    it is that :func:`~agentjobs.attention.owes_notification` stops returning true the
    moment the episode is acknowledged or the waiting set empties. A phone in a tunnel
    therefore keeps being tried for as long as the work is still stopped on its owner,
    and stops the moment it is not -- which is the behaviour a count would have had to
    approximate.
    """
    if subscription.consecutive_failures <= 0 or subscription.last_attempt_at is None:
        return True
    wait = min(2**subscription.consecutive_failures, BACKOFF_CAP_SECONDS)
    return now >= subscription.last_attempt_at + timedelta(seconds=wait)


def _record(
    subscription: Subscription, result: SendResult, payload: PushPayload, now: datetime
) -> Subscription:
    """Fold one attempt's outcome into the device row.

    ``last_episode_id`` is written for ``sent`` and for ``rejected`` and **not** for
    ``transient``. That is the whole retry rule in one line: a delivered push is done
    with, a refused one will be refused again, and an unreachable service is worth
    another try while the person is still waited on.
    """
    remembered = (
        payload.episode_id
        if result.outcome in ("sent", "rejected")
        else subscription.last_episode_id
    )
    return replace(
        subscription,
        last_episode_id=remembered,
        last_attempt_at=now,
        last_status=result.status,
        last_error=result.error,
        consecutive_failures=(
            0 if result.outcome == "sent" else subscription.consecutive_failures + 1
        ),
    )


@dataclass(frozen=True)
class RoundResult:
    """What one pass over one project's devices did. Everything the watcher reports."""

    project_id: str
    episode_id: Optional[str]
    blocking: int
    attempted: int = 0
    sent: int = 0
    pruned: int = 0
    failed: int = 0

    @property
    def quiet(self) -> bool:
        return self.attempted == 0

    def describe(self) -> str:
        parts = [f"{self.sent} sent"]
        if self.pruned:
            parts.append(f"{self.pruned} expired endpoint(s) forgotten")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return f"{self.project_id}: {', '.join(parts)} for episode {self.episode_id}"


def deliver_round(
    manager: TaskManager,
    project_id: str,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
    client: Optional[httpx.Client] = None,
    key: Optional[VapidKey] = None,
) -> RoundResult:
    """Reconcile this project's attention and push it to whichever devices are owed one.

    **Reconciling here is what makes push independent of a browser.** The desktop
    notifier advances the episode by polling from a page; if that were the only
    reconcile, a person who closed the laptop and walked away would be woken by
    nothing, which is precisely the case this task exists for. ``reconcile`` is
    idempotent, so the watcher and any number of open pages advancing the same episode
    cannot manufacture attention between them.

    A project with no devices does no work beyond that reconcile, and a project with
    devices that are all up to date does no network at all.
    """
    moment = now or datetime.now(tz=timezone.utc)
    subscriptions = load(project_id, home=home)
    if not subscriptions:
        # Still reconcile: an episode that opened while nobody was subscribed is real,
        # and a device registered a minute later must not be told about it as if new.
        state = reconcile(manager, project_id, home=home, now=moment)
        return RoundResult(
            project_id=project_id,
            episode_id=state.episode.id if state.episode else None,
            blocking=state.blocking,
        )

    state = reconcile(manager, project_id, home=home, now=moment)
    owed = [
        row
        for row in subscriptions
        if owes_notification(state.episode, row.last_episode_id) and _retry_due(row, moment)
    ]
    if not owed:
        return RoundResult(
            project_id=project_id,
            episode_id=state.episode.id if state.episode else None,
            blocking=state.blocking,
        )

    vapid = key or load_or_create(home=home)
    owned_client = client is None
    http = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    updates: Dict[str, Subscription] = {}
    gone: List[str] = []
    sent = failed = 0
    try:
        for row in owed:
            payload = payload_for(state, project_id, detail=row.detail)
            if payload is None:
                continue
            result = send(row, payload.encode(project_id), key=vapid, client=http)
            if result.outcome == "gone":
                gone.append(row.id)
                continue
            updates[row.id] = _record(row, result, payload, moment)
            if result.outcome == "sent":
                sent += 1
            else:
                failed += 1
    finally:
        if owned_client:
            http.close()

    def apply(rows: Tuple[Subscription, ...]) -> Sequence[Subscription]:
        return [updates.get(row.id, row) for row in rows if row.id not in gone]

    mutate(project_id, apply, home=home)
    return RoundResult(
        project_id=project_id,
        episode_id=state.episode.id if state.episode else None,
        blocking=state.blocking,
        attempted=len(owed),
        sent=sent,
        pruned=len(gone),
        failed=failed,
    )


def send_test(
    project_id: str,
    subscription: Subscription,
    *,
    home: Optional[Path] = None,
    client: Optional[httpx.Client] = None,
    key: Optional[VapidKey] = None,
    now: Optional[datetime] = None,
) -> SendResult:
    """Push one message to one device on purpose, without touching the episode.

    The answer to "did opting in actually work", which nothing else can give: a device
    that subscribed while the waiting set was empty would otherwise learn whether push
    reaches it only at the moment it matters. Deliberately **not** recorded as a
    notification for the current episode -- a test must not be able to consume the
    interruption a real episode is owed.
    """
    moment = now or datetime.now(tz=timezone.utc)
    payload = PushPayload(
        episode_id="test",
        blocking=0,
        title="AgentJobs push is working",
        body="This device will be woken when work stops on you.",
        url=f"/app/p/{project_id}/tasks?status=human",
        tag=notification_tag(project_id),
    )
    vapid = key or load_or_create(home=home)
    owned_client = client is None
    http = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
    try:
        result = send(subscription, payload.encode(project_id), key=vapid, client=http)
    finally:
        if owned_client:
            http.close()

    def apply(rows: Tuple[Subscription, ...]) -> Sequence[Subscription]:
        return [
            (
                replace(
                    row,
                    last_attempt_at=moment,
                    last_status=result.status,
                    last_error=result.error,
                    consecutive_failures=(
                        0 if result.outcome == "sent" else row.consecutive_failures + 1
                    ),
                )
                if row.id == subscription.id
                else row
            )
            for row in rows
            if not (result.outcome == "gone" and row.id == subscription.id)
        ]

    mutate(project_id, apply, home=home)
    return result
