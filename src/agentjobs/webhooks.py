"""Webhook management and event dispatch for AgentJobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine, Dict, List, Optional

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from .models import Task

logger = logging.getLogger(__name__)


class Webhook(BaseModel):
    """Webhook configuration for task event notifications."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Unique webhook identifier.")
    url: HttpUrl = Field(..., description="Target URL for webhook delivery.")
    events: List[str] = Field(..., description="List of events to trigger this webhook.")
    secret: str = Field(..., description="Secret for HMAC signature verification.")
    active: bool = Field(default=True, description="Whether this webhook is active.")
    created: datetime = Field(..., description="When the webhook was created.")
    last_triggered: Optional[datetime] = Field(
        default=None, description="Last time this webhook was successfully triggered."
    )

    def record_trigger(self) -> None:
        """Record that this webhook was triggered."""
        self.last_triggered = datetime.now(tz=timezone.utc)


class WebhookStorage:
    """YAML-based webhook storage."""

    def __init__(self, webhooks_path: Path):
        """Initialize webhook storage with path to webhooks.yaml file."""
        self.webhooks_path = Path(webhooks_path)
        self.webhooks_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.webhooks_path.exists():
            self._write_webhooks([])

    def _read_webhooks(self) -> List[dict]:
        """Read webhooks from YAML file."""
        try:
            content = self.webhooks_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or []
        except yaml.YAMLError as exc:  # pragma: no cover
            logger.error("Failed to parse webhooks YAML: %s", exc)
            return []
        return data if isinstance(data, list) else []

    def _write_webhooks(self, webhooks: List[dict]) -> None:
        """Write webhooks to YAML file."""
        yaml_text = yaml.safe_dump(webhooks, sort_keys=False, allow_unicode=False)
        self.webhooks_path.write_text(yaml_text, encoding="utf-8")

    def list_webhooks(self) -> List[Webhook]:
        """List all webhooks."""
        webhooks: List[Webhook] = []
        for data in self._read_webhooks():
            try:
                webhook = Webhook.model_validate(data)
                webhooks.append(webhook)
            except ValidationError as exc:  # pragma: no cover
                logger.error("Validation error loading webhook: %s", exc)
        return webhooks

    def get_webhook(self, webhook_id: str) -> Optional[Webhook]:
        """Get webhook by ID."""
        for data in self._read_webhooks():
            if data.get("id") == webhook_id:
                try:
                    return Webhook.model_validate(data)
                except ValidationError as exc:  # pragma: no cover
                    logger.error("Validation error loading webhook %s: %s", webhook_id, exc)
                    return None
        return None

    def save_webhook(self, webhook: Webhook) -> Webhook:
        """Save or update a webhook."""
        webhooks = self._read_webhooks()
        webhooks = [w for w in webhooks if w.get("id") != webhook.id]
        webhooks.append(webhook.model_dump(mode="json"))
        self._write_webhooks(webhooks)
        return webhook

    def create_webhook(
        self,
        url: str,
        events: List[str],
        secret: str,
        active: bool = True,
    ) -> Webhook:
        """Create a new webhook."""
        webhook = Webhook(
            id=f"wh_{uuid.uuid4().hex[:10]}",
            url=url,
            events=events,
            secret=secret,
            active=active,
            created=datetime.now(tz=timezone.utc),
        )
        return self.save_webhook(webhook)

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        webhooks = self._read_webhooks()
        original_count = len(webhooks)
        webhooks = [w for w in webhooks if w.get("id") != webhook_id]
        if len(webhooks) < original_count:
            self._write_webhooks(webhooks)
            return True
        return False


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
        webhooks = [hook for hook in self.list_webhooks() if hook.active and event in hook.events]
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

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        """Schedule coroutine in background thread or existing event loop.

        Typed as Coroutine rather than Awaitable because that is what both branches
        below actually require: asyncio.run and loop.create_task reject a bare
        awaitable. Every caller already passes a coroutine.
        """
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
