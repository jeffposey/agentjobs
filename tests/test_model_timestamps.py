"""Regression test for the model method that stamps a timestamp.

`Webhook.record_trigger` referenced `timezone.utc` while its module imported only
`datetime`, so it raised ``NameError: name 'timezone' is not defined`` on every call.
It had no test at all, which is exactly why it went unnoticed: the coverage report
listed the broken lines as uncovered.

The test exists to call the method. A one-line import fix without it would leave the
same hole open for the next method that reaches for something the module does not
import. Its twin, `Comment.update_content`, went with the v1 models in schema v2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentjobs.webhooks import Webhook


@pytest.fixture()
def webhook() -> Webhook:
    """A webhook that has never fired."""
    return Webhook(
        id="webhook-1",
        url="https://example.com/hook",
        events=["task.handoff"],
        secret="shhh",
        created=datetime.now(tz=timezone.utc) - timedelta(hours=1),
    )


class TestWebhookRecordTrigger:
    def test_does_not_raise(self, webhook: Webhook) -> None:
        # The bug's real consequence was here: record_trigger is called by
        # WebhookManager._dispatch immediately after a successful POST, so every
        # successful delivery raised inside a detached background task -- invisibly --
        # and the save_webhook call on the next line never ran.
        webhook.record_trigger()

    def test_stamps_an_aware_utc_timestamp(self, webhook: Webhook) -> None:
        before = datetime.now(tz=timezone.utc)

        webhook.record_trigger()

        assert webhook.last_triggered is not None
        assert webhook.last_triggered.tzinfo is not None
        assert webhook.last_triggered >= before

    def test_last_triggered_starts_unset(self, webhook: Webhook) -> None:
        assert webhook.last_triggered is None
