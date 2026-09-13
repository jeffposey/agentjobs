"""Idle Claude sessions on this machine: the report, the record, and the switch (task-447).

Mounted once, unscoped, for the same reason as ``/api/runs``: the processes sharing this
machine's Claude login are not a fact about any one project.

The report is computed on request rather than cached, because the question it answers is
"what would be stopped *now*", and a cached answer to that is the one kind that misleads.
It never includes command lines: a dispatched session's argv carries its whole prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from agentjobs.dispatch.config import (
    DispatchConfigError,
    DispatchNotConfiguredError,
    IdleSessionSettings,
    load_dispatch_config,
    set_idle_session_settings,
)
from agentjobs.dispatch.idle_sessions import (
    SWEEP_INTERVAL_SECONDS,
    auth_incident_count,
    book_for,
    take_inventory,
)
from agentjobs.execution.errors import ExecutionStoreError
from agentjobs.projects import default_home

router = APIRouter(prefix="/api/sessions", tags=["dispatch"])


class IdleSessionSettingsView(BaseModel):
    configured: bool = Field(..., description="This machine has a dispatch config to hold them.")
    enforce: bool = Field(..., description="Idle, resumable sessions are stopped automatically.")
    idle_minutes: int
    max_stops_per_sweep: int
    sweep_interval_seconds: int


class IdleSessionView(BaseModel):
    pid: int
    kind: str = Field(..., description="daemon, background, remote_control_host, ...")
    verdict: str = Field(..., description="protected, in_use, idle, report_only or candidate.")
    reason: str
    session_id: Optional[str] = None
    short_id: Optional[str] = None
    name: str = ""
    cwd: str = ""
    ledger_status: Optional[str] = None
    run_id: Optional[str] = None
    last_activity: Optional[str] = None
    idle_seconds: Optional[int] = None
    resume_commands: List[str] = Field(default_factory=list)


class IdleSessionEventView(BaseModel):
    event_id: str
    kind: str = Field(..., description="stop, or mode for a change of enforcement.")
    at: str
    session_id: Optional[str] = None
    outcome: str = Field(..., description="stopped, failed or declined; enforce or report.")
    detail: str = ""
    trigger: str = ""
    name: str = ""
    cwd: str = ""
    short_id: Optional[str] = None
    last_activity: Optional[str] = None
    idle_seconds: Optional[int] = None
    reason: str = ""
    resume_commands: List[str] = Field(default_factory=list)
    auth_incidents: Optional[int] = Field(
        None, description="On a mode event: the auth incident count at the switch-over."
    )


class IdleSessionsView(BaseModel):
    settings: IdleSessionSettingsView
    generated_at: str
    sessions: List[IdleSessionView]
    errors: List[str] = Field(default_factory=list)
    events: List[IdleSessionEventView]
    auth_incidents: Optional[int] = None


class IdleSessionSettingsUpdate(BaseModel):
    enforce: Optional[bool] = None
    idle_minutes: Optional[int] = Field(None, gt=0)


def _home() -> Path:
    return default_home()


def _settings_view(home: Path) -> IdleSessionSettingsView:
    config = load_dispatch_config(home)
    settings = config.idle_sessions if config is not None else IdleSessionSettings()
    return IdleSessionSettingsView(
        configured=config is not None,
        enforce=settings.enforce,
        idle_minutes=settings.idle_minutes,
        max_stops_per_sweep=settings.max_stops_per_sweep,
        sweep_interval_seconds=SWEEP_INTERVAL_SECONDS,
    )


def _events(home: Path) -> tuple[List[IdleSessionEventView], Optional[int]]:
    try:
        book = book_for(home)
        events = book.events()
        count = auth_incident_count(book.store)
    except ExecutionStoreError:
        return [], None
    views = []
    for event in events:
        detail = event.detail
        views.append(
            IdleSessionEventView(
                event_id=event.event_id,
                kind=event.kind,
                at=event.at,
                session_id=event.session_id,
                outcome=event.outcome,
                detail=str(detail.get("detail") or ""),
                trigger=str(detail.get("trigger") or ""),
                name=str(detail.get("name") or ""),
                cwd=str(detail.get("cwd") or ""),
                short_id=detail.get("short_id"),
                last_activity=detail.get("last_activity"),
                idle_seconds=detail.get("idle_seconds"),
                reason=str(detail.get("reason") or ""),
                resume_commands=list(detail.get("resume_commands") or []),
                auth_incidents=detail.get("auth_incidents"),
            )
        )
    return views, count


@router.get("/idle", response_model=IdleSessionsView)
async def list_idle_sessions() -> IdleSessionsView:
    """Every Claude Code process on this machine, what the sweep makes of it, and its record."""
    home = _home()
    settings = _settings_view(home)
    try:
        inventory = await asyncio.to_thread(
            take_inventory, home, idle_minutes=settings.idle_minutes
        )
        sessions = [
            IdleSessionView(
                **{key: value for key, value in view.as_dict().items() if key in _SESSION_FIELDS}
            )
            for view in inventory.sessions
        ]
        errors = inventory.errors
        generated_at = inventory.generated_at
    except (OSError, ValueError) as exc:
        from datetime import datetime, timezone

        sessions, errors = [], [f"inventory: {exc}"]
        generated_at = datetime.now(timezone.utc).isoformat()
    events, count = _events(home)
    return IdleSessionsView(
        settings=settings,
        generated_at=generated_at,
        sessions=sessions,
        errors=errors,
        events=events,
        auth_incidents=count,
    )


_SESSION_FIELDS = set(IdleSessionView.model_fields)


@router.put("/idle/settings", response_model=IdleSessionSettingsView)
async def update_idle_session_settings(body: IdleSessionSettingsUpdate) -> IdleSessionSettingsView:
    """Switch enforcement on or off, or change the threshold. Stops nothing by itself."""
    home = _home()
    if body.enforce is None and body.idle_minutes is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to change.")
    try:
        settings = set_idle_session_settings(
            enforce=body.enforce, idle_minutes=body.idle_minutes, home=home
        )
    except DispatchNotConfiguredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except DispatchConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    try:
        from agentjobs.dispatch.idle_sessions import note_mode

        note_mode(book_for(home), enforce=settings.enforce, idle_minutes=settings.idle_minutes)
    except ExecutionStoreError:
        pass  # the poller's next sweep records the switch instead
    return _settings_view(home)
