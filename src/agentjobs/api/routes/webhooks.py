"""Webhook management endpoints."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

from agentjobs.models import Webhook
from agentjobs.webhooks import WebhookManager

from ..dependencies import get_webhook_manager

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


class WebhookCreateRequest(BaseModel):
    """Request to create a webhook."""

    url: HttpUrl = Field(..., description="Webhook target URL")
    events: List[str] = Field(..., description="Events to subscribe to")
    secret: str = Field(..., description="Secret for HMAC verification")
    active: bool = Field(default=True, description="Whether webhook is active")


@router.get("", response_model=List[Webhook])
async def list_webhooks(
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
) -> List[Webhook]:
    """List all registered webhooks."""
    return webhook_manager.list_webhooks()


@router.get("/{webhook_id}", response_model=Webhook)
async def get_webhook(
    webhook_id: str,
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
) -> Webhook:
    """Get a specific webhook by ID."""
    webhook = webhook_manager.get_webhook(webhook_id)
    if webhook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook {webhook_id} not found",
        )
    return webhook


@router.post("", response_model=Webhook, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: WebhookCreateRequest,
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
) -> Webhook:
    """Create a new webhook."""
    return webhook_manager.create_webhook(
        url=str(payload.url),
        events=payload.events,
        secret=payload.secret,
        active=payload.active,
    )


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: str,
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
) -> None:
    """Delete a webhook."""
    success = webhook_manager.delete_webhook(webhook_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook {webhook_id} not found",
        )


@router.post("/{webhook_id}/test", response_model=Dict[str, Any])
async def test_webhook(
    webhook_id: str,
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
) -> Dict[str, Any]:
    """Send a test event to a webhook."""
    try:
        webhook_manager.test_webhook(webhook_id)
        return {"status": "ok", "message": f"Test event sent to webhook {webhook_id}"}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
