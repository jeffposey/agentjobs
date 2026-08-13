"""Assertions for API details consumed by generated clients."""

from agentjobs.api.main import app


def test_scoped_routes_declare_project_id_path_parameter() -> None:
    operation = app.openapi()["paths"]["/api/projects/{project_id}/tasks"]["get"]

    assert any(
        parameter["name"] == "project_id"
        and parameter["in"] == "path"
        and parameter["required"] is True
        for parameter in operation["parameters"]
    )
