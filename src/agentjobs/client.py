"""Python client for interacting with the AgentJobs REST API (schema v2)."""

from __future__ import annotations

from enum import Enum
from types import TracebackType
from typing import Any, Dict, List, Optional

from urllib.parse import quote

import httpx

from .models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome, Priority, Task


class TaskClientError(RuntimeError):
    """Raised when the REST API returns an error or connection fails."""


class TaskClient:
    """High-level convenience client for the AgentJobs REST API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        *,
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialise the client with the API base URL and timeout."""
        self._base_url = base_url.rstrip("/") or "http://localhost:8765"
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=timeout,
                transport=transport,
            )

    @property
    def base_url(self) -> str:
        """The service URL this client talks to, for diagnostics and error text."""
        return self._base_url

    # ------------------------------------------------------------------
    # Service metadata
    # ------------------------------------------------------------------
    def service_health(self) -> Dict[str, Any]:
        """Return the service health payload, raising when it is unreachable."""
        response = self._request("GET", "/api/health")
        payload: Dict[str, Any] = response.json()
        return payload

    def service_version(self) -> Dict[str, Any]:
        """Return the service's AgentJobs version and served task schema version."""
        response = self._request("GET", "/api/version")
        payload: Dict[str, Any] = response.json()
        return payload

    def list_projects(self) -> List[Dict[str, Any]]:
        """List every project the service serves, as raw records.

        Deliberately untyped at this layer. The typed, project-scoped surface the MCP
        tools consume is built on top of this by the project/actor routing work; this
        method exists so the MCP startup probe can prove the endpoint answers.
        """
        response = self._request("GET", "/api/projects")
        payload: List[Dict[str, Any]] = response.json()
        return payload

    # ------------------------------------------------------------------
    # Context manager helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying httpx client when owned."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TaskClient":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_tasks(
        self,
        lifecycle: Optional[Lifecycle | str] = None,
        ball: Optional[Ball | str] = None,
        priority: Optional[Priority | str] = None,
    ) -> List[Task]:
        """List tasks filtered along the state axes.

        ``ball="human"`` is the human inbox; ``ball="external"`` the blocked list.
        """
        params: Dict[str, str] = {}
        if lifecycle is not None:
            params["lifecycle"] = self._enum_to_str(lifecycle)
        if ball is not None:
            params["ball"] = self._enum_to_str(ball)
        if priority is not None:
            params["priority"] = self._enum_to_str(priority)
        response = self._request("GET", "/api/tasks", params=params)
        payload = response.json()
        return [self._parse_task(item) for item in payload]

    def get_task(self, task_id: str) -> Task:
        """Fetch a task by identifier."""
        response = self._request("GET", f"/api/tasks/{task_id}")
        return self._parse_task(response.json())

    def get_next_task(
        self,
        priority: Optional[Priority | str] = None,
        *,
        agent: Optional[str] = None,
    ) -> Optional[Task]:
        """Return the next claimable task or None when unavailable."""
        params: Dict[str, str] = {}
        if priority is not None:
            params["priority"] = self._enum_to_str(priority)
        if agent is not None:
            params["agent"] = agent
        response = self._request("GET", "/api/tasks/next", params=params)
        payload = response.json()
        if payload is None:
            return None
        return self._parse_task(payload)

    def search_tasks(self, query: str) -> List[Task]:
        """Search for tasks by query string."""
        if not query.strip():
            raise TaskClientError("Query must not be empty")
        response = self._request("GET", "/api/search", params={"q": query})
        return [self._parse_task(item) for item in response.json()]

    # ------------------------------------------------------------------
    # Creation and edits
    # ------------------------------------------------------------------
    def create_task(
        self,
        *,
        title: str,
        description: str,
        priority: Priority | str = Priority.MEDIUM,
        category: str = "general",
        **kwargs: Any,
    ) -> Task:
        """Create a new task record."""
        payload: Dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": self._enum_to_str(priority),
            "category": category,
        }
        payload.update(self._serialise_payload(kwargs))
        response = self._request("POST", "/api/tasks", json=payload)
        return self._parse_task(response.json())

    def update_task(self, task_id: str, **updates: Any) -> Task:
        """Partially update a task. State axes move through the verbs below."""
        if not updates:
            raise TaskClientError("No updates provided")
        payload = self._serialise_payload(updates)
        response = self._request(
            "PATCH",
            f"/api/tasks/{task_id}",
            json=payload,
        )
        return self._parse_task(response.json())

    def mark_deliverable_complete(self, task_id: str, *, deliverable_path: str) -> Task:
        """Mark a deliverable path as done."""
        encoded_path = quote(deliverable_path, safe="")
        response = self._request(
            "PATCH",
            f"/api/tasks/{task_id}/deliverables/{encoded_path}",
        )
        return self._parse_task(response.json())

    # ------------------------------------------------------------------
    # The state verbs (the canonical loop)
    # ------------------------------------------------------------------
    def claim_task(self, task_id: str, *, agent: str) -> Task:
        """Claim a ready task. One winner; everyone else gets an error."""
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/claim",
            json={"agent": agent},
        )
        return self._parse_task(response.json())

    def handoff_task(
        self,
        task_id: str,
        *,
        actor: str,
        ball: Ball | str,
        ball_reason: BallReason | str,
        ball_prompt: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Task:
        """Move the ball, with its ask."""
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/handoff",
            json={
                "actor": actor,
                "ball": self._enum_to_str(ball),
                "ball_reason": self._enum_to_str(ball_reason),
                "ball_prompt": ball_prompt,
                "body": body,
            },
        )
        return self._parse_task(response.json())

    def release_task(self, task_id: str, *, actor: str, body: Optional[str] = None) -> Task:
        """Return a claimed task to the pool."""
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/release",
            json={"actor": actor, "body": body},
        )
        return self._parse_task(response.json())

    def close_task(
        self,
        task_id: str,
        *,
        actor: str,
        outcome: Outcome | str = Outcome.COMPLETED,
        body: Optional[str] = None,
        archive: bool = False,
    ) -> Task:
        """End the task with an outcome."""
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/close",
            json={
                "actor": actor,
                "outcome": self._enum_to_str(outcome),
                "body": body,
                "archive": archive,
            },
        )
        return self._parse_task(response.json())

    # ------------------------------------------------------------------
    # The log
    # ------------------------------------------------------------------
    def add_log_entry(
        self,
        task_id: str,
        *,
        actor: str,
        type: LogEntryType | str = LogEntryType.NOTE,
        body: Optional[str] = None,
        re: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """Append a note/progress/decision/question/answer/instruction entry."""
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/log",
            json={
                "actor": actor,
                "type": self._enum_to_str(type),
                "body": body,
                "re": re,
                "data": data or {},
            },
        )
        return self._parse_task(response.json())

    def add_progress_update(
        self,
        task_id: str,
        *,
        summary: str,
        details: Optional[str] = None,
        agent: str = "",
    ) -> Task:
        """Append a progress update entry."""
        payload = {
            "author": agent or "system",
            "summary": summary,
            "details": details,
        }
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/progress",
            json=payload,
        )
        return self._parse_task(response.json())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _serialise_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise enum values so callers may pass enums or strings."""
        serialised: Dict[str, Any] = {}
        for key, value in payload.items():
            if value is None:
                serialised[key] = None
                continue
            if isinstance(value, Enum):
                serialised[key] = self._enum_to_str(value)
            elif isinstance(value, list):
                serialised[key] = [
                    self._enum_to_str(item) if isinstance(item, Enum) else item for item in value
                ]
            else:
                serialised[key] = value
        return serialised

    def _parse_task(self, data: Dict[str, Any]) -> Task:
        # display_status is computed on the server and computed again here; the model
        # rejects it as an input field.
        data.pop("display_status", None)
        return Task.model_validate(data)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            detail = self._extract_error_detail(exc.response)
            raise TaskClientError(detail) from exc
        except httpx.RequestError as exc:
            raise TaskClientError(f"Request failed: {exc}") from exc

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return detail or f"HTTP {response.status_code}"

    @staticmethod
    def _enum_to_str(value: Enum | str) -> str:
        return value.value if isinstance(value, Enum) else str(value)
