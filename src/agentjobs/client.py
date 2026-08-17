"""Python client for interacting with the AgentJobs REST API (schema v2)."""

from __future__ import annotations

from enum import Enum
from types import TracebackType
from typing import Any, Dict, List, Optional

from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from .models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome, Priority, Task


class ProjectActor(BaseModel):
    """One actor a project's configuration defines."""

    id: str
    kind: str
    display_name: str


class ProjectSummary(BaseModel):
    """A project as discovery reports it.

    Mirrors the REST ``ProjectResponse``. Defining it here rather than importing the
    API model keeps the client free of any dependency on the server package; a
    contract test asserts the two carry the same fields, so the duplication cannot
    drift silently.
    """

    id: str
    name: str
    root: str
    tasks_directory: str
    task_count: Optional[int] = None
    actors: List[ProjectActor] = Field(default_factory=list)
    default_user: Optional[str] = None

    @property
    def actor_ids(self) -> List[str]:
        """Configured actor ids, for validating an actor and for naming the choices."""
        return [actor.id for actor in self.actors]


class TaskClientError(RuntimeError):
    """Raised when the REST API returns an error or connection fails.

    ``status_code`` is the HTTP status when the service answered, and ``None`` when
    it could not be reached at all. Callers need the difference: a 404 from a route
    that should exist means the service is the wrong version, which is a completely
    different repair from a refused connection.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        """Record the message and, when the service answered, its status."""
        super().__init__(message)
        self.status_code = status_code


class TaskClient:
    """High-level convenience client for the AgentJobs REST API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        *,
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        project_id: Optional[str] = None,
    ) -> None:
        """Initialise the client with the API base URL and timeout.

        ``project_id`` addresses one project explicitly. Prefer
        :meth:`for_project` over passing it here; the two are equivalent, but the
        method reads as a scoping operation on an existing connection.
        """
        self._base_url = base_url.rstrip("/") or "http://localhost:8765"
        self._project_id = project_id
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

    @property
    def project_id(self) -> Optional[str]:
        """The project this client addresses, or None for the default project."""
        return self._project_id

    # ------------------------------------------------------------------
    # Project scoping
    # ------------------------------------------------------------------
    def for_project(self, project_id: str) -> "TaskClient":
        """Return a client addressing one project by exact id.

        Route construction lives here rather than in callers so no caller -- an MCP
        tool least of all -- has to assemble ``/api/projects/<id>/...`` itself and get
        the escaping right. The returned client shares this one's HTTP connection and
        does not own it, so closing it does not close the parent.

        Scoping is a new object, never a mutation of this one. A client with a
        settable "current project" is exactly the hidden state that lets a session
        which moved between repositories write to the wrong one.
        """
        if not project_id:
            raise TaskClientError("A project id is required; it is never inferred.")
        scoped = TaskClient(
            self._base_url,
            client=self._client,
            project_id=project_id,
        )
        # The parent owns the connection. Sharing without transferring ownership is
        # what makes scoping cheap enough to do per call.
        scoped._owns_client = False
        return scoped

    def _path(self, suffix: str) -> str:
        """Build a request path, project-scoped when this client is scoped."""
        if self._project_id is None:
            return f"/api{suffix}"
        return f"/api/projects/{quote(self._project_id, safe='')}{suffix}"

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

        Kept untyped for the MCP startup probe, which checks that required fields are
        *present* and so must see whatever the service actually sent. Callers that
        want the parsed form should use :meth:`projects`.
        """
        response = self._request("GET", "/api/projects")
        payload: List[Dict[str, Any]] = response.json()
        return payload

    def projects(self) -> List[ProjectSummary]:
        """Every project the service serves, with its actor vocabulary."""
        return [ProjectSummary.model_validate(item) for item in self.list_projects()]

    def get_project(self, project_id: str) -> ProjectSummary:
        """Return one project by exact id, naming the valid ids when it is unknown.

        The registry has no single-project read route, so this filters the list. That
        keeps the "unknown project" message able to name the alternatives, which a
        bare 404 from a per-project route could not do.
        """
        available = self.projects()
        for project in available:
            if project.id == project_id:
                return project
        known = ", ".join(sorted(item.id for item in available)) or "(none registered)"
        raise TaskClientError(
            f"Unknown project {project_id!r}. Projects on this service: {known}.",
            status_code=404,
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
        response = self._request("GET", self._path("/tasks"), params=params)
        payload = response.json()
        return [self._parse_task(item) for item in payload]

    def get_task(self, task_id: str) -> Task:
        """Fetch a task by identifier."""
        response = self._request("GET", self._path(f"/tasks/{task_id}"))
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
        response = self._request("GET", self._path("/tasks/next"), params=params)
        payload = response.json()
        if payload is None:
            return None
        return self._parse_task(payload)

    def search_tasks(self, query: str) -> List[Task]:
        """Search for tasks by query string."""
        if not query.strip():
            raise TaskClientError("Query must not be empty")
        response = self._request("GET", self._path("/search"), params={"q": query})
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
        response = self._request("POST", self._path("/tasks"), json=payload)
        return self._parse_task(response.json())

    def update_task(self, task_id: str, **updates: Any) -> Task:
        """Partially update a task. State axes move through the verbs below."""
        if not updates:
            raise TaskClientError("No updates provided")
        payload = self._serialise_payload(updates)
        response = self._request(
            "PATCH",
            self._path(f"/tasks/{task_id}"),
            json=payload,
        )
        return self._parse_task(response.json())

    def mark_deliverable_complete(self, task_id: str, *, deliverable_path: str) -> Task:
        """Mark a deliverable path as done."""
        encoded_path = quote(deliverable_path, safe="")
        response = self._request(
            "PATCH",
            self._path(f"/tasks/{task_id}/deliverables/{encoded_path}"),
        )
        return self._parse_task(response.json())

    # ------------------------------------------------------------------
    # The state verbs (the canonical loop)
    # ------------------------------------------------------------------
    def claim_task(self, task_id: str, *, agent: str) -> Task:
        """Claim a ready task. One winner; everyone else gets an error."""
        response = self._request(
            "POST",
            self._path(f"/tasks/{task_id}/claim"),
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
            self._path(f"/tasks/{task_id}/handoff"),
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
            self._path(f"/tasks/{task_id}/release"),
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
            self._path(f"/tasks/{task_id}/close"),
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
            self._path(f"/tasks/{task_id}/log"),
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
            self._path(f"/tasks/{task_id}/progress"),
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
            raise TaskClientError(detail, status_code=exc.response.status_code) from exc
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
