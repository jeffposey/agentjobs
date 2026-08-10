"""Regression tests for the two model methods that stamp a timestamp.

Both `Comment.update_content` and `Webhook.record_trigger` referenced `timezone.utc`
while models.py imported only `datetime`, so both raised
``NameError: name 'timezone' is not defined`` on every call. Neither had a single test,
which is exactly why it went unnoticed: the coverage report listed the broken lines as
uncovered.

These tests exist to call both methods. A one-line import fix without them would leave
the same hole open for the next method that reaches for something models.py does not
import.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentjobs.models import Comment
from agentjobs.webhooks import Webhook


@pytest.fixture()
def comment() -> Comment:
    """A comment created an hour ago, so an update is detectably newer."""
    created = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    return Comment(
        id="comment-1",
        task_id="task-001",
        author="jeff",
        content="original",
        created=created,
    )


@pytest.fixture()
def webhook() -> Webhook:
    """A webhook that has never fired."""
    return Webhook(
        id="webhook-1",
        url="https://example.com/hook",
        events=["task.status_changed"],
        secret="shhh",
        created=datetime.now(tz=timezone.utc) - timedelta(hours=1),
    )


class TestCommentUpdateContent:
    def test_does_not_raise(self, comment: Comment) -> None:
        comment.update_content("revised")

    def test_replaces_the_content(self, comment: Comment) -> None:
        comment.update_content("revised")

        assert comment.content == "revised"

    def test_stamps_an_aware_utc_timestamp(self, comment: Comment) -> None:
        before = datetime.now(tz=timezone.utc)

        comment.update_content("revised")

        assert comment.updated is not None
        assert comment.updated.tzinfo is not None, "a naive timestamp cannot be compared"
        assert comment.updated >= before

    def test_updated_moves_ahead_of_created(self, comment: Comment) -> None:
        comment.update_content("revised")

        assert comment.updated is not None
        assert comment.updated > comment.created


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
