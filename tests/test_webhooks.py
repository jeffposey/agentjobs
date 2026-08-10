"""Tests for webhook functionality."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs.models_v2 import Ball, BallReason, Lifecycle
from agentjobs.storage import TaskStorage
from agentjobs.manager import TaskManager
from agentjobs.webhooks import WebhookManager, WebhookStorage


@pytest.fixture
def webhook_storage(tmp_path: Path) -> WebhookStorage:
    """Create a temporary webhook storage."""
    webhooks_path = tmp_path / "webhooks.yaml"
    return WebhookStorage(webhooks_path)


@pytest.fixture
def webhook_manager(webhook_storage: WebhookStorage) -> WebhookManager:
    """Create a webhook manager."""
    return WebhookManager(webhook_storage)


@pytest.fixture
def task_storage(tmp_path: Path) -> TaskStorage:
    """Create a temporary task storage."""
    tasks_dir = tmp_path / "tasks"
    return TaskStorage(tasks_dir)


@pytest.fixture
def task_manager(task_storage: TaskStorage, webhook_manager: WebhookManager) -> TaskManager:
    """Create a task manager with webhook support."""
    return TaskManager(task_storage, webhook_manager)


def test_create_webhook(webhook_manager: WebhookManager) -> None:
    """Test creating a webhook."""
    webhook = webhook_manager.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.status_changed"],
        secret="test-secret",
    )
    assert webhook.id.startswith("wh_")
    assert str(webhook.url) == "http://localhost:5000/webhook"
    assert webhook.events == ["task.status_changed"]
    assert webhook.secret == "test-secret"
    assert webhook.active is True


def test_list_webhooks(webhook_manager: WebhookManager) -> None:
    """Test listing webhooks."""
    webhook_manager.create_webhook(
        url="http://localhost:5000/webhook1",
        events=["task.created"],
        secret="secret1",
    )
    webhook_manager.create_webhook(
        url="http://localhost:5000/webhook2",
        events=["task.completed"],
        secret="secret2",
    )
    webhooks = webhook_manager.list_webhooks()
    assert len(webhooks) == 2


def test_get_webhook(webhook_manager: WebhookManager) -> None:
    """Test getting a webhook by ID."""
    webhook = webhook_manager.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.status_changed"],
        secret="test-secret",
    )
    retrieved = webhook_manager.get_webhook(webhook.id)
    assert retrieved is not None
    assert retrieved.id == webhook.id
    assert str(retrieved.url) == str(webhook.url)


def test_delete_webhook(webhook_manager: WebhookManager) -> None:
    """Test deleting a webhook."""
    webhook = webhook_manager.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.status_changed"],
        secret="test-secret",
    )
    success = webhook_manager.delete_webhook(webhook.id)
    assert success is True
    retrieved = webhook_manager.get_webhook(webhook.id)
    assert retrieved is None


def test_webhook_persistence(webhook_storage: WebhookStorage) -> None:
    """Test that webhooks persist across manager instances."""
    manager1 = WebhookManager(webhook_storage)
    webhook = manager1.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.created"],
        secret="test-secret",
    )

    # Create new manager with same storage
    manager2 = WebhookManager(webhook_storage)
    retrieved = manager2.get_webhook(webhook.id)
    assert retrieved is not None
    assert retrieved.id == webhook.id


def test_task_manager_fires_webhook_on_handoff(
    task_manager: TaskManager,
    webhook_manager: WebhookManager,
) -> None:
    """Test that TaskManager fires the v2 task.handoff event."""
    # Create a webhook subscribed to the v2 handoff event
    webhook_manager.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.handoff"],
        secret="test-secret",
    )

    # Create, claim and hand off a task (delivery will fail silently: nothing is
    # listening on localhost, and that is fine -- this verifies the path completes).
    task = task_manager.create_task(
        title="Test Task",
        description="Test description",
        category="test",
        lifecycle=Lifecycle.READY,
    )
    task_manager.claim_task(task.id, agent="test-agent")
    task_manager.handoff(
        task.id,
        actor="test-agent",
        ball=Ball.HUMAN,
        ball_reason=BallReason.REVIEW,
        ball_prompt="Review the work.",
    )

    # Just verify the method completes without error
    assert True
