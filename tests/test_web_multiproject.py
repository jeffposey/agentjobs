"""Tests for the project-scoped web UI.

Reuses the two-projects-sharing-a-task-id shape from test_api_multiproject, because the
bug this feature invites is a link that drops its project id and silently opens the
wrong project's task.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.projects import ProjectRegistry

# Absolute, not relative: tests/ has no __init__.py, so pytest imports these as
# top-level modules and a relative import has no parent package to resolve against.
from test_api_multiproject import SHARED_TASK_ID, build_project


@pytest.fixture()
def two_projects_web(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path]]:
    """Two registered projects, a temp registry, and a cwd inside neither of them."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    build_project(tmp_path / "alpha", "Alpha")
    build_project(tmp_path / "beta", "Beta")
    registry = ProjectRegistry(home=tmp_path / "home")
    registry.add(tmp_path / "alpha", project_id="alpha")
    registry.add(tmp_path / "beta", project_id="beta")

    # follow_redirects=False so the redirect itself is assertable, not just its target.
    with TestClient(app, follow_redirects=False) as client:
        yield client, tmp_path

    reset_dependency_cache()


class TestScopedPages:
    @pytest.mark.parametrize("path", ["/", "/tasks", f"/tasks/{SHARED_TASK_ID}"])
    def test_scoped_pages_render(self, two_projects_web, path: str) -> None:
        client, _ = two_projects_web

        assert client.get(f"/p/alpha{path}").status_code == 200

    def test_each_project_renders_its_own_task_despite_the_shared_id(
        self, two_projects_web
    ) -> None:
        client, _ = two_projects_web

        alpha = client.get(f"/p/alpha/tasks/{SHARED_TASK_ID}").text
        beta = client.get(f"/p/beta/tasks/{SHARED_TASK_ID}").text

        assert "Alpha task" in alpha and "Beta task" not in alpha
        assert "Beta task" in beta and "Alpha task" not in beta

    def test_task_list_shows_only_the_scoped_project(self, two_projects_web) -> None:
        client, _ = two_projects_web

        assert "Beta task" not in client.get("/p/alpha/tasks").text

    def test_unknown_project_is_404(self, two_projects_web) -> None:
        client, _ = two_projects_web

        assert client.get("/p/ghost/tasks").status_code == 404


class TestFilterAttributesAreUsable:
    """The task list filters on data- attributes, so their *values* have to be right.

    This shipped broken. `{{ task.lifecycle }}` rendered `Lifecycle.READY` under Python
    3.11's mixin-enum change, so every filter matched nothing and the dashboard's
    "View all N waiting tasks" link led to an empty page -- while the status badge in
    the same row rendered correctly, because it compares rather than interpolates.

    Asserting the rendered attribute, rather than that the attribute merely exists, is
    the difference between catching this and not: the original check looked for
    `data-ball=` and passed.
    """

    def test_filter_attributes_hold_bare_vocabulary_values(self, two_projects_web) -> None:
        client, _ = two_projects_web

        body = client.get("/p/alpha/tasks").text

        assert 'data-lifecycle="ready"' in body
        assert 'data-lifecycle="Lifecycle.' not in body
        assert 'data-ball="agent"' in body
        assert 'data-ball="Ball.' not in body
        assert 'data-priority="high"' in body
        assert 'data-priority="Priority.' not in body

    def test_the_dashboard_waiting_link_targets_a_filter_that_matches(
        self, two_projects_web
    ) -> None:
        # The reported symptom, end to end: the dashboard links to ?status=human, and
        # a task whose ball is human must carry data-ball="human" for that to select it.
        # Four waiting tasks, because the "View all N" link only appears past three --
        # which is why the broken link was invisible until the backlog grew.
        client, _ = two_projects_web
        for index in range(4):
            created = client.post(
                "/api/projects/alpha/tasks",
                json={
                    "title": f"Waiting {index}",
                    "description": "x",
                    "category": "ops",
                    "lifecycle": "ready",
                },
            ).json()
            client.post(f"/api/projects/alpha/tasks/{created['id']}/claim", json={"agent": "codex"})
            client.post(
                f"/api/projects/alpha/tasks/{created['id']}/handoff",
                json={
                    "actor": "codex",
                    "ball": "human",
                    "ball_reason": "review",
                    "ball_prompt": "Look at this.",
                },
            )

        dashboard = client.get("/p/alpha/").text
        assert "?status=human" in dashboard

        listing = client.get("/p/alpha/tasks?status=human").text
        assert 'data-ball="human"' in listing
        # And the select offers the value the link uses, or the page loads pre-filtered
        # to an option that is not there.
        assert '<option value="human">' in listing


class TestLinksCarryTheirProject:
    """A link that drops its project id opens a different project's task."""

    @pytest.mark.parametrize("path", ["/", "/tasks", f"/tasks/{SHARED_TASK_ID}"])
    def test_no_unscoped_task_links_are_emitted(self, two_projects_web, path: str) -> None:
        client, _ = two_projects_web

        body = client.get(f"/p/beta{path}").text

        assert 'href="/tasks' not in body
        assert "href='/tasks" not in body
        assert "window.location.href='/tasks" not in body

    def test_review_actions_address_the_project(self, two_projects_web) -> None:
        # task-059 deferred this: the buttons posted to the unscoped /api/tasks/...,
        # which only resolved correctly because of the server's working directory.
        client, _ = two_projects_web
        client.post(
            f"/api/projects/beta/tasks/{SHARED_TASK_ID}/claim",
            json={"agent": "codex"},
        )
        client.post(
            f"/api/projects/beta/tasks/{SHARED_TASK_ID}/handoff",
            json={
                "actor": "codex",
                "ball": "human",
                "ball_reason": "review",
                "ball_prompt": "review me",
            },
        )

        body = client.get(f"/p/beta/tasks/{SHARED_TASK_ID}").text

        assert "/api/projects/beta/tasks/" in body
        assert "fetch('/api/tasks/" not in body


class TestLegacyUrls:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/", "/p/beta"),
            ("/tasks", "/p/beta/tasks"),
            (f"/tasks/{SHARED_TASK_ID}", f"/p/beta/tasks/{SHARED_TASK_ID}"),
        ],
    )
    def test_unscoped_urls_redirect_into_the_resolved_default(
        self, two_projects_web, monkeypatch, path: str, expected: str
    ) -> None:
        client, tmp_path = two_projects_web
        monkeypatch.chdir(tmp_path / "beta")

        response = client.get(path)

        assert response.status_code == 307
        assert response.headers["location"] == expected

    def test_query_string_survives_the_redirect(self, two_projects_web, monkeypatch) -> None:
        client, tmp_path = two_projects_web
        monkeypatch.chdir(tmp_path / "alpha")

        response = client.get("/tasks?status=ready")

        assert response.headers["location"] == "/p/alpha/tasks?status=ready"

    def test_ambiguous_default_offers_a_picker_rather_than_guessing(self, two_projects_web) -> None:
        # cwd is inside neither project and two are registered. Picking one would mean
        # silently showing another project's work.
        client, _ = two_projects_web

        response = client.get("/")

        assert response.status_code == 307
        assert response.headers["location"] == "/projects"

    def test_picker_lists_every_project(self, two_projects_web) -> None:
        client, _ = two_projects_web

        body = client.get("/projects").text

        assert "Choose a project" in body
        assert "Alpha" in body and "Beta" in body


class TestSwitcher:
    def test_switcher_lists_both_projects_on_every_page(self, two_projects_web) -> None:
        client, _ = two_projects_web

        for path in ("/", "/tasks", f"/tasks/{SHARED_TASK_ID}"):
            body = client.get(f"/p/alpha{path}").text
            assert "/p/alpha/" in body and "/p/beta/" in body, path

    def test_switcher_names_the_active_project(self, two_projects_web) -> None:
        client, _ = two_projects_web

        assert "Beta" in client.get("/p/beta/").text

    def test_switcher_links_to_project_onboarding(self, two_projects_web) -> None:
        client, _ = two_projects_web

        assert 'href="/projects/new"' in client.get("/p/alpha/").text


class TestSingleProjectCompatibility:
    def test_env_pinned_install_still_serves_the_legacy_urls(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An install with nothing registered must behave as it always did."""
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv(TASKS_DIR_ENV, str(tmp_path / "solo" / "tasks"))
        monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path / "solo"))
        reset_dependency_cache()
        build_project(tmp_path / "solo", "Solo")

        with TestClient(app) as client:
            # Redirects followed here: the point is that the page still arrives.
            response = client.get("/tasks")
            assert response.status_code == 200
            assert "Solo task" in response.text

        reset_dependency_cache()
