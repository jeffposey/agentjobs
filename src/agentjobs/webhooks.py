"""Webhook management and event dispatch for AgentJobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Dict, List, Optional

import httpx

from .models import Task, Webhook
from .storage import WebhookStorage

logger = logging.getLogger(__name__)


class WebhookManager:
    """Manage webhook lifecycle and dispatch events."""

    def __init__(self, storage: WebhookStorage):
        """Initialize webhook manager with storage."""
        self.storage = storage

    def list_webhooks(self) -> List[Webhook]:
        """List all webhooks."""
        return self.storage.list_webhooks()

    def create_webhook(
        self,
        url: str,
        events: List[str],
        secret: str,
        active: bool = True,
    ) -> Webhook:
        """Create a new webhook."""
        return self.storage.create_webhook(
            url=url,
            events=events,
            secret=secret,
            active=active,
        )

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        return self.storage.delete_webhook(webhook_id)

    def get_webhook(self, webhook_id: str) -> Optional[Webhook]:
        """Get webhook by ID."""
        return self.storage.get_webhook(webhook_id)

    def fire_event(
        self,
        event: str,
        task: Task,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fire a webhook event asynchronously for all matching webhooks."""
        metadata = metadata or {}
        webhooks = [
            hook
            for hook in self.list_webhooks()
            if hook.active and event in hook.events
        ]
        if not webhooks:
            return

        payload = self._build_payload(event=event, task=task, metadata=metadata)
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        for webhook in webhooks:
            signature = self._compute_signature(payload_text, webhook.secret)
            coro = self._dispatch(webhook, payload_text, signature)
            self._schedule(coro)

    def test_webhook(self, webhook_id: str) -> None:
        """Send a test webhook event."""
        webhook = self.get_webhook(webhook_id)
        if webhook is None:
            raise ValueError(f"Webhook '{webhook_id}' not found.")

        payload = {
            "event": "webhook.test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": {},
            "triggered_by": "system",
            "action": "test",
        }
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        signature = self._compute_signature(payload_text, webhook.secret)
        asyncio.run(self._dispatch(webhook, payload_text, signature))

    def _schedule(self, coro: Awaitable[None]) -> None:
        """Schedule coroutine in background thread or existing event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop - create thread to run it
            threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()
            return
        # Running loop exists - create task
        loop.create_task(coro)

    async def _dispatch(
        self,
        webhook: Webhook,
        payload_text: str,
        signature: str,
    ) -> None:
        """Dispatch webhook HTTP request asynchronously."""
        headers = {
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    str(webhook.url),
                    headers=headers,
                    content=payload_text,
                )
                response.raise_for_status()
        except Exception as exc:  # pragma: no cover - external network call
            logger.warning("Failed to deliver webhook %s: %s", webhook.id, exc)
            return

        webhook.record_trigger()
        self.storage.save_webhook(webhook)

    def _build_payload(
        self,
        event: str,
        task: Task,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build webhook payload."""
        payload: Dict[str, Any] = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task": task.model_dump(mode="json"),
        }
        payload.update(metadata)
        return payload

    def _compute_signature(self, payload: str, secret: str) -> str:
        """Compute HMAC-SHA256 signature for webhook payload."""
        digest = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return digest
