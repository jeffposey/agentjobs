"""Tests for FastAPI application scaffolding."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentjobs.api.main import app

client = TestClient(app)


def test_health_check_endpoint() -> None:
    """Health endpoint returns OK payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
