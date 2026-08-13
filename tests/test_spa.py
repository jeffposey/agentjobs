"""Production serving contract for the React single-page application."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentjobs.api.spa import register_spa


SHELL = "<!doctype html><html><body><div id='root'></div></body></html>"


def spa_client(dist: Path) -> TestClient:
    app = FastAPI()
    register_spa(app, dist)
    return TestClient(app)


def test_spa_shell_is_served_at_root_and_direct_deep_link(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(SHELL, encoding="utf-8")

    with spa_client(tmp_path) as client:
        root = client.get("/app")
        deep_link = client.get("/app/p/agentjobs/tasks/task-042-example")

    assert root.status_code == 200
    assert root.text == SHELL
    assert deep_link.status_code == 200
    assert deep_link.text == SHELL


def test_spa_assets_are_served_as_files_not_as_the_shell(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(SHELL, encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("window.agentjobs = true;", encoding="utf-8")

    with spa_client(tmp_path) as client:
        response = client.get("/app/assets/app.js")

    assert response.status_code == 200
    assert response.text == "window.agentjobs = true;"
    assert response.text != SHELL


def test_spa_reports_missing_build_without_breaking_app_import(tmp_path: Path) -> None:
    with spa_client(tmp_path) as client:
        response = client.get("/app")

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "React frontend is not built; run `npm run build` in frontend/."
    )
