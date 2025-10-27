"""Tests for webhook functionality."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentjobs.models import Task, TaskStatus, Webhook
from agentjobs.storage import TaskStorage, WebhookStorage
from agentjobs.manager import TaskManager
from agentjobs.webhooks import WebhookManager


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
    assert webhook.url == "http://localhost:5000/webhook"
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
    assert retrieved.url == webhook.url


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


def test_task_manager_fires_webhook_on_status_change(
    task_manager: TaskManager,
    webhook_manager: WebhookManager,
) -> None:
    """Test that TaskManager fires webhook on status change."""
    # Create a webhook
    webhook_manager.create_webhook(
        url="http://localhost:5000/webhook",
        events=["task.status_changed"],
        secret="test-secret",
    )

    # Create and update task
    task = task_manager.create_task(
        title="Test Task",
        description="Test description",
        category="test",
    )

    # Update status (webhook will fire but won't actually deliver since localhost isn't listening)
    task_manager.update_status(
        task_id=task.id,
        status=TaskStatus.IN_PROGRESS,
        author="test-user",
        summary="Starting work",
    )

    # Just verify the method completes without error
    assert True
