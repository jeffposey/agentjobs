"""Python client for interacting with the AgentJobs REST API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from urllib.parse import quote

import httpx

from .models import Priority, Task, TaskStatus


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

    # ------------------------------------------------------------------
    # Context manager helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying httpx client when owned."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TaskClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------
    def list_tasks(
        self,
        status: Optional[TaskStatus | str] = None,
        priority: Optional[Priority | str] = None,
    ) -> List[Task]:
        """List tasks with optional workflow and priority filters."""
        params: Dict[str, str] = {}
        if status is not None:
            params["status_filter"] = self._enum_to_str(status)
        if priority is not None:
            params["priority_filter"] = self._enum_to_str(priority)
        response = self._request("GET", "/api/tasks", params=params)
        payload = response.json()
        return [self._parse_task(item) for item in payload]

    def get_task(self, task_id: str) -> Task:
        """Fetch a task by identifier."""
        response = self._request("GET", f"/api/tasks/{task_id}")
        return self._parse_task(response.json())

    def get_next_task(self, priority: Optional[Priority | str] = None) -> Optional[Task]:
        """Return the next planned task or None when unavailable."""
        params: Dict[str, str] = {}
        if priority is not None:
            params["priority"] = self._enum_to_str(priority)
        response = self._request("GET", "/api/tasks/next", params=params)
        payload = response.json()
        if payload is None:
            return None
        return self._parse_task(payload)

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
        """Partially update a task."""
        if not updates:
            raise TaskClientError("No updates provided")
        payload = self._serialise_payload(updates)
        response = self._request(
            "PATCH",
            f"/api/tasks/{task_id}",
            json=payload,
        )
        return self._parse_task(response.json())

    def mark_in_progress(
        self,
        task_id: str,
        *,
        agent: str,
        summary: str = "",
    ) -> Task:
        """Update a task status to in progress."""
        return self._post_status(
            task_id,
            status=TaskStatus.IN_PROGRESS,
            author=agent,
            summary=summary or "Marked in progress",
        )

    def mark_completed(
        self,
        task_id: str,
        *,
        agent: str = "",
        summary: str = "",
    ) -> Task:
        """Mark a task as completed."""
        return self._post_status(
            task_id,
            status=TaskStatus.COMPLETED,
            author=agent or "system",
            summary=summary or "Task completed",
        )

    def mark_blocked(
        self,
        task_id: str,
        *,
        reason: str,
        agent: str = "",
    ) -> Task:
        """Mark a task as blocked with a reason."""
        summary = f"Blocked: {reason}" if reason else "Blocked"
        return self._post_status(
            task_id,
            status=TaskStatus.BLOCKED,
            author=agent or "system",
            summary=summary,
            details=reason or None,
        )

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

    def mark_deliverable_complete(self, task_id: str, *, deliverable_path: str) -> Task:
        """Mark a deliverable path as completed."""
        encoded_path = quote(deliverable_path, safe="")
        response = self._request(
            "PATCH",
            f"/api/tasks/{task_id}/deliverables/{encoded_path}",
        )
        return self._parse_task(response.json())

    def get_starter_prompt(self, task_id: str) -> str:
        """Return the starter prompt text for a task."""
        response = self._request(
            "GET",
            f"/api/tasks/{task_id}/prompts/starter",
        )
        payload = response.json()
        return payload.get("starter", "")

    def add_followup_prompt(
        self,
        task_id: str,
        *,
        content: str,
        author: str,
        context: str = "",
    ) -> Task:
        """Append a follow-up prompt for a task."""
        payload = {
            "author": author,
            "content": content,
            "context": context or None,
        }
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/prompts",
            json=payload,
        )
        return self._parse_task(response.json())

    def search_tasks(self, query: str) -> List[Task]:
        """Search for tasks by query string."""
        if not query.strip():
            raise TaskClientError("Query must not be empty")
        response = self._request("GET", "/api/search", params={"q": query})
        return [self._parse_task(item) for item in response.json()]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _post_status(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        author: str,
        summary: str,
        details: Optional[str] = None,
    ) -> Task:
        payload = {
            "status": self._enum_to_str(status),
            "author": author or "system",
            "summary": summary,
            "details": details,
        }
        response = self._request(
            "POST",
            f"/api/tasks/{task_id}/status",
            json=payload,
        )
        return self._parse_task(response.json())

    def _serialise_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalise enum values and filter None values."""
        serialised: Dict[str, Any] = {}
        for key, value in payload.items():
            if value is None:
                serialised[key] = None
                continue
            if isinstance(value, (Priority, TaskStatus)):
                serialised[key] = self._enum_to_str(value)
            elif isinstance(value, list):
                serialised[key] = [
                    self._enum_to_str(item) if isinstance(item, (Priority, TaskStatus)) else item
                    for item in value
                ]
            else:
                serialised[key] = value
        return serialised

    def _parse_task(self, data: Dict[str, Any]) -> Task:
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
    def _enum_to_str(value: Priority | TaskStatus | str) -> str:
        return value.value if hasattr(value, "value") else str(value)
