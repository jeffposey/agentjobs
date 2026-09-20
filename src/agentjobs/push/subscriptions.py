"""The devices a person has opted in, and what happened to each one (task-423).

``~/.agentjobs/push/<project>.yaml``, beside the attention episode rather than in the
task store, for the same three reasons: it is machine state about a person, it must not
travel in a clone or an export, and it is shared by every client this server has.

**A subscription is a capability, not a name.** Its ``endpoint`` is a URL that anybody
holding it can send a notification to, so it is treated the way a secret is treated: it
is never returned by the API, never logged, and never put in a task record. What the
API answers with is :meth:`Subscription.view`, which carries an opaque id, the push
service's host and a label the person can recognise their own phone by.

**The endpoint is also the identity.** Subscribing twice from one device -- a reload, a
reinstall, a ``pushsubscriptionchange`` that happened to produce the same endpoint --
replaces the row rather than adding one, so a person with two phones has two rows and
never four.

**Delivery outcomes live on the row.** Not in a separate log: the question a person
asks is "is my phone still getting these", which is a property of the device, and a log
would answer it only by being read backwards. The last status, the last error and the
consecutive-failure count are the whole of the answer, and
:mod:`agentjobs.push.delivery` is what writes them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ..attention import SAFE_PROJECT_ID
from ..dispatch.atomic_yaml import merge_yaml_atomically, read_yaml_resiliently
from ..projects import default_home
from .keys import PUSH_DIRNAME

DETAIL_COUNT = "count"
"""Privacy mode, opt-in per device. The push withholds the task's id and title.

It still carries the *ask* -- "Needs review" -- because that is what is wanted rather
than what the work is, and a push that says only a number does not tell a person
whether to get up. See :mod:`agentjobs.push.delivery`.
"""

DETAIL_TASK = "task"
"""Default. The push names the lead task as well as the ask.

**This reverses the default task-423 shipped**, on the owner's decision of 2026-09-20
(task-421). That default withheld the name because a push lands on a lock screen, in a
hallway, on a watch -- sound reasoning, and it belongs to a product whose users are not
the person who installed it. This one is a single consumer on his own phone over his
own tailnet, and the cautious default cost him the usefulness of every notification he
received. A device with bystanders turns privacy on.
"""

DETAIL_MODES = (DETAIL_COUNT, DETAIL_TASK)

DETAIL_DEFAULT = DETAIL_TASK
"""What a device gets when it says nothing, and what an unreadable value falls back to."""

UNHEALTHY_AFTER = 10
"""Consecutive failures before a device is reported as unhealthy.

It is reported, not removed. A push service having a bad hour, a laptop closed for a
week, a phone in a tunnel: all of them fail repeatedly and all of them recover, and a
subscription deleted on a count would have to be re-created by hand by somebody who
never learned it was gone. The only thing that deletes a row is the push service saying
the endpoint is gone -- see :mod:`agentjobs.push.delivery`.
"""


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _moment(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Subscription:
    """One browser on one device that has asked to be woken."""

    id: str
    endpoint: str
    p256dh: str
    auth: str
    label: str = ""
    detail: str = DETAIL_DEFAULT
    created_at: datetime = field(default_factory=_now)
    last_episode_id: Optional[str] = None
    last_attempt_at: Optional[datetime] = None
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0

    @property
    def service(self) -> str:
        """The push service's host -- ``fcm.googleapis.com`` and friends.

        Shown instead of the endpoint. It is enough to tell an Android device from an
        Apple one at a glance, and it grants nothing to whoever reads it.
        """
        return urlsplit(self.endpoint).netloc

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures < UNHEALTHY_AFTER

    def view(self) -> Dict[str, Any]:
        """The redacted form the API returns. The endpoint is deliberately absent."""
        return {
            "id": self.id,
            "service": self.service,
            "label": self.label,
            "detail": self.detail,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "healthy": self.healthy,
        }

    def to_document(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "endpoint": self.endpoint,
            "p256dh": self.p256dh,
            "auth": self.auth,
            "label": self.label,
            "detail": self.detail,
            "created_at": self.created_at.isoformat(),
            "last_episode_id": self.last_episode_id,
            "last_attempt_at": (self.last_attempt_at.isoformat() if self.last_attempt_at else None),
            "last_status": self.last_status,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }


def _from_document(document: Any) -> Optional[Subscription]:
    """Read one row back, or ``None`` for anything that is not a usable subscription.

    A row missing any of the three fields a push needs is not repairable and not worth
    keeping: without the endpoint there is nowhere to send, and without the keys the
    body cannot be encrypted. Dropping it silently is right because the device can
    re-subscribe in one click and a half-row cannot be made whole from here.
    """
    if not isinstance(document, dict):
        return None
    endpoint = document.get("endpoint")
    p256dh = document.get("p256dh")
    auth = document.get("auth")
    if not (isinstance(endpoint, str) and isinstance(p256dh, str) and isinstance(auth, str)):
        return None
    if not endpoint or not p256dh or not auth:
        return None
    detail = document.get("detail")
    created = _moment(document.get("created_at"))
    failures = document.get("consecutive_failures")
    status = document.get("last_status")
    return Subscription(
        id=str(document.get("id") or new_subscription_id()),
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        label=str(document.get("label") or ""),
        detail=detail if detail in DETAIL_MODES else DETAIL_DEFAULT,
        created_at=created or _now(),
        last_episode_id=(
            str(document["last_episode_id"]) if document.get("last_episode_id") else None
        ),
        last_attempt_at=_moment(document.get("last_attempt_at")),
        last_status=status if isinstance(status, int) else None,
        last_error=(str(document["last_error"]) if document.get("last_error") else None),
        consecutive_failures=failures if isinstance(failures, int) and failures >= 0 else 0,
    )


def new_subscription_id() -> str:
    return f"dev_{uuid.uuid4().hex[:12]}"


def subscriptions_path(project_id: str, *, home: Optional[Path] = None) -> Path:
    """The device document for one project.

    Guarded by the same filename rule as the attention state, from the same constant,
    because it is the same question asked about the same directory -- and because the
    reserved ``_local`` id has already cost this repository one round of 500s for
    having two opinions about it.
    """
    if not SAFE_PROJECT_ID.match(project_id):
        raise ValueError(f"project id {project_id!r} cannot be used as a filename")
    return (home or default_home()) / PUSH_DIRNAME / f"{project_id}.yaml"


def load(project_id: str, *, home: Optional[Path] = None) -> Tuple[Subscription, ...]:
    """Every device registered for this project, in the order they were written."""
    document = read_yaml_resiliently(subscriptions_path(project_id, home=home))
    return _read_rows(document if isinstance(document, dict) else {})


def _read_rows(document: Dict[str, Any]) -> Tuple[Subscription, ...]:
    rows = document.get("subscriptions")
    if not isinstance(rows, list):
        return ()
    parsed = (_from_document(row) for row in rows)
    return tuple(row for row in parsed if row is not None)


def mutate(
    project_id: str,
    change: Callable[[Tuple[Subscription, ...]], Sequence[Subscription]],
    *,
    home: Optional[Path] = None,
) -> Tuple[Subscription, ...]:
    """Apply *change* to the stored devices as one read-modify-replace.

    Under the same merge lock the attention state uses. It matters more here than
    there: the watcher records a delivery result for every device in a round while the
    person may be adding or removing one from the page, and a lost update would either
    resurrect a device they removed or lose the episode id that stops a second push.
    """
    path = subscriptions_path(project_id, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    result: List[Tuple[Subscription, ...]] = []

    def merge(current: Dict[str, Any], _fields: Dict[str, Any]) -> Dict[str, Any]:
        updated = tuple(change(_read_rows(current)))
        result.append(updated)
        return {"subscriptions": [row.to_document() for row in updated]}

    merge_yaml_atomically(path, {}, merge=merge)
    return result[0] if result else ()


def upsert(
    project_id: str,
    subscription: Subscription,
    *,
    home: Optional[Path] = None,
) -> Subscription:
    """Register a device, replacing any row with the same endpoint.

    The replacement keeps the **existing** row's id and ``last_episode_id``. Keeping the
    id means a page that was already showing this device does not see it jump; keeping
    the episode id means re-subscribing during an open episode does not re-arm a push
    the person has already had.
    """
    stored: List[Subscription] = []

    def change(rows: Tuple[Subscription, ...]) -> Sequence[Subscription]:
        kept = [row for row in rows if row.endpoint != subscription.endpoint]
        previous = next((row for row in rows if row.endpoint == subscription.endpoint), None)
        merged = subscription
        if previous is not None:
            merged = replace(
                subscription,
                id=previous.id,
                created_at=previous.created_at,
                last_episode_id=previous.last_episode_id,
            )
        stored.append(merged)
        return kept + [merged]

    mutate(project_id, change, home=home)
    return stored[0]


def remove(
    project_id: str,
    *,
    subscription_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    home: Optional[Path] = None,
) -> bool:
    """Forget a device. By id from the page, by endpoint from the browser.

    Both forms are idempotent: removing something that is not there is not an error,
    because the two ways a person unsubscribes -- the button here and the browser's own
    permission reset -- routinely both fire for one device.
    """
    removed: List[bool] = []

    def change(rows: Tuple[Subscription, ...]) -> Sequence[Subscription]:
        kept = [
            row
            for row in rows
            if not (
                (subscription_id is not None and row.id == subscription_id)
                or (endpoint is not None and row.endpoint == endpoint)
            )
        ]
        removed.append(len(kept) != len(rows))
        return kept

    mutate(project_id, change, home=home)
    return bool(removed and removed[0])
