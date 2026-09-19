"""Mobile push for the attention episode (task-423).

The half of :mod:`agentjobs.attention` that reaches a person who has left the building.
It adds no policy: the episode, the acknowledgment and the reset are the ones task-422
settled, and this package is a second kind of client for them -- one whose
last-notified id lives on a subscription row instead of in a browser's storage.

Four layers, each testable without the one above it:

* :mod:`~agentjobs.push.webpush` -- :rfc:`8291` encryption, with the RFC's own worked
  example as its test.
* :mod:`~agentjobs.push.keys` -- the machine's :rfc:`8292` VAPID keypair. Public half
  to the browser, private half never.
* :mod:`~agentjobs.push.subscriptions` -- the devices, and what happened to each.
* :mod:`~agentjobs.push.delivery` -- what a push says and who is owed one.

and :mod:`~agentjobs.push.watcher`, the loop in the server's lifespan that makes all of
it work with no page open.

``docs/push.md`` is the prose, including the platform table and why there is no push
client library in ``pyproject.toml``.
"""

from .delivery import (
    PushPayload,
    RoundResult,
    SendResult,
    deliver_round,
    notification_tag,
    payload_for,
    send,
    send_test,
)
from .keys import VapidKey, load_or_create, vapid_path
from .subscriptions import (
    DETAIL_COUNT,
    DETAIL_MODES,
    DETAIL_TASK,
    Subscription,
    load,
    new_subscription_id,
    remove,
    subscriptions_path,
    upsert,
)

__all__ = [
    "DETAIL_COUNT",
    "DETAIL_MODES",
    "DETAIL_TASK",
    "PushPayload",
    "RoundResult",
    "SendResult",
    "Subscription",
    "VapidKey",
    "deliver_round",
    "load",
    "load_or_create",
    "new_subscription_id",
    "notification_tag",
    "payload_for",
    "remove",
    "send",
    "send_test",
    "subscriptions_path",
    "upsert",
    "vapid_path",
]
