"""The review panel's document route: a Markdown deliverable read at its branch's head.

task-594. Every refusal is asserted on its code *and* on the sentence a reviewer reads,
because the panel shows that sentence in place of the document, and a refusal that says
nothing useful is an empty section with extra steps.

The repository is real: ``git show``-shaped reads are the whole mechanism, and a stub of
git would test the stub.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.deliverable_text import MAX_DELIVERABLE_BYTES, unsafe_path_reason

DOC = "# The design\n\nOne **decision** per section.\n"


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    (tmp_path / "tasks").mkdir()
    config_dir = tmp_path / ".agentjobs"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Test",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "claude", "kind": "agent"},
                ],
                "default_user": "Jeff Posey",
            }
        ),
        encoding="utf-8",
    )
    run_git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def commit_on_branch(root: Path, branch: str, files: Dict[str, str]) -> str:
    """Commit ``files`` on ``branch`` without touching the checked-out tree's branch."""
    run_git(root, "checkout", "-q", "-b", branch)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        run_git(root, "add", name)
    run_git(root, "commit", "-q", "-m", f"work on {branch}")
    head = run_git(root, "rev-parse", "HEAD")
    run_git(root, "checkout", "-q", "main")
    return head


def make_task(
    client: TestClient,
    *,
    deliverables: List[str],
    branches: List[Dict[str, str]],
) -> str:
    created = client.post(
        "/api/tasks",
        json={"title": "A design", "description": "spec", "actor": "claude"},
    )
    assert created.status_code == 201, created.text
    task: Dict[str, Any] = created.json()
    patched = client.patch(
        f"/api/tasks/{task['id']}",
        json={
            "deliverables": [{"path": path} for path in deliverables],
            "branches": branches,
        },
    )
    assert patched.status_code == 200, patched.text
    return str(task["id"])


def active(name: str) -> Dict[str, str]:
    return {"name": name, "status": "active"}


def refusal(response: Any, status_code: int, code: str) -> str:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code
    assert body["message"] == body["detail"]
    return str(body["message"])


def test_a_document_is_read_at_the_head_of_its_branch(client: TestClient, tmp_path: Path) -> None:
    head = commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])

    response = client.get(f"/api/tasks/{task_id}/deliverables/0")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == DOC
    assert body["path"] == "docs/design.md"
    assert body["branch"] == "feat/design"
    assert body["commit"] == head[:8]
    assert body["size"] == len(DOC.encode("utf-8"))
    # The checked-out tree is main, which does not have the file: the read came from
    # the branch, not the working directory.
    assert not (tmp_path / "docs" / "design.md").exists()


def test_the_project_scoped_mount_serves_it_too(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])
    projects = client.get("/api/projects").json()
    project_id = projects[0]["id"]

    response = client.get(f"/api/projects/{project_id}/tasks/{task_id}/deliverables/0")

    assert response.status_code == 200, response.text
    assert response.json()["text"] == DOC


def test_a_later_commit_is_what_is_shown(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    run_git(tmp_path, "checkout", "-q", "feat/design")
    (tmp_path / "docs" / "design.md").write_text("# Revised\n", encoding="utf-8")
    run_git(tmp_path, "commit", "-q", "-am", "revise")
    run_git(tmp_path, "checkout", "-q", "main")
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])

    assert client.get(f"/api/tasks/{task_id}/deliverables/0").json()["text"] == "# Revised\n"


def test_an_index_past_the_list_is_refused(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])

    message = refusal(
        client.get(f"/api/tasks/{task_id}/deliverables/1"), 404, "no_such_deliverable"
    )
    assert "there is no deliverable 1" in message
    refusal(client.get(f"/api/tasks/{task_id}/deliverables/-1"), 404, "no_such_deliverable")


def test_a_path_the_caller_writes_never_reaches_git(client: TestClient, tmp_path: Path) -> None:
    """There is no route that takes a path at all -- only an index."""
    commit_on_branch(
        tmp_path, "feat/design", {"docs/design.md": DOC, "secret.md": "SECRET-CONTENT\n"}
    )
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])

    response = client.get(f"/api/tasks/{task_id}/deliverables/secret.md")
    # The index parameter is an int: a path is a validation failure before any handler.
    assert response.status_code == 400
    assert "SECRET-CONTENT" not in response.text


def test_a_file_that_is_not_markdown_is_refused(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"src/app.py": "print('x')\n"})
    task_id = make_task(client, deliverables=["src/app.py"], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 422, "not_markdown")
    assert message == "src/app.py is not a Markdown file; only .md deliverables are rendered."


@pytest.mark.parametrize(
    ("path", "says"),
    [
        ("/etc/passwd.md", "is an absolute path"),
        ("C:/Windows/notes.md", "is an absolute path"),
        ("../outside.md", "'..' component"),
        ("docs/../../outside.md", "'..' component"),
        ("./docs/design.md", "'..' component"),
        ("docs//design.md", "empty, '.' or '..' component"),
        ("docs\\design.md", "contains a backslash"),
    ],
)
def test_an_absolute_or_escaping_path_is_refused(
    client: TestClient, tmp_path: Path, path: str, says: str
) -> None:
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    task_id = make_task(client, deliverables=[path], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 422, "unsafe_path")
    assert says in message


def test_a_control_character_in_the_path_is_refused() -> None:
    assert unsafe_path_reason("docs/de\nsign.md") == (
        "The deliverable's path contains a control character."
    )
    assert unsafe_path_reason("") == "The deliverable's path is empty."
    assert unsafe_path_reason("docs/design.md") is None


def test_an_oversize_file_is_refused_with_its_size(client: TestClient, tmp_path: Path) -> None:
    big = "x" * (MAX_DELIVERABLE_BYTES + 1)
    commit_on_branch(tmp_path, "feat/design", {"docs/big.md": big})
    task_id = make_task(client, deliverables=["docs/big.md"], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 413, "too_large")
    assert message.startswith(f"docs/big.md is {MAX_DELIVERABLE_BYTES + 1:,} bytes, over the")


def test_a_file_exactly_at_the_limit_is_served(client: TestClient, tmp_path: Path) -> None:
    exact = "x" * MAX_DELIVERABLE_BYTES
    commit_on_branch(tmp_path, "feat/design", {"docs/big.md": exact})
    task_id = make_task(client, deliverables=["docs/big.md"], branches=[active("feat/design")])

    response = client.get(f"/api/tasks/{task_id}/deliverables/0")
    assert response.status_code == 200
    assert response.json()["size"] == MAX_DELIVERABLE_BYTES


def test_a_symlink_is_not_followed(client: TestClient, tmp_path: Path) -> None:
    """A symlink is a blob too; its mode is what tells it apart."""
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    run_git(tmp_path, "checkout", "-q", "feat/design")
    target = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=tmp_path,
        input="docs/design.md",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    run_git(tmp_path, "update-index", "--add", "--cacheinfo", f"120000,{target},link.md")
    run_git(tmp_path, "commit", "-q", "-m", "a symlink")
    run_git(tmp_path, "reset", "-q", "--hard")
    run_git(tmp_path, "checkout", "-q", "main")
    task_id = make_task(client, deliverables=["link.md"], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 422, "not_a_regular_file")
    assert "git mode 120000" in message


def test_a_directory_named_like_a_document_is_refused(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"notes.md/inner.txt": "x\n"})
    task_id = make_task(client, deliverables=["notes.md"], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 422, "not_a_regular_file")
    assert "tree" in message


def test_no_active_branch_is_named(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/design", {"docs/design.md": DOC})
    task_id = make_task(
        client,
        deliverables=["docs/design.md"],
        branches=[{"name": "feat/design", "status": "merged"}],
    )

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 409, "no_active_branch")
    assert "lists no active branch" in message


def test_several_active_branches_are_named(client: TestClient, tmp_path: Path) -> None:
    commit_on_branch(tmp_path, "feat/one", {"docs/design.md": DOC})
    commit_on_branch(tmp_path, "feat/two", {"docs/design.md": DOC})
    task_id = make_task(
        client,
        deliverables=["docs/design.md"],
        branches=[active("feat/one"), active("feat/two")],
    )

    message = refusal(
        client.get(f"/api/tasks/{task_id}/deliverables/0"), 409, "several_active_branches"
    )
    assert "2 active branches (feat/one, feat/two)" in message


def test_a_branch_that_does_not_exist_is_named(client: TestClient) -> None:
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/gone")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 404, "branch_missing")
    assert "Branch 'feat/gone' does not exist" in message


def test_a_branch_named_like_an_option_is_not_an_option(client: TestClient) -> None:
    task_id = make_task(
        client, deliverables=["docs/design.md"], branches=[active("--output=/tmp/x")]
    )

    refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 404, "branch_missing")


def test_a_missing_file_is_named(client: TestClient, tmp_path: Path) -> None:
    head = commit_on_branch(tmp_path, "feat/design", {"docs/other.md": DOC})
    task_id = make_task(client, deliverables=["docs/design.md"], branches=[active("feat/design")])

    message = refusal(client.get(f"/api/tasks/{task_id}/deliverables/0"), 404, "file_missing")
    assert message == f"docs/design.md does not exist on feat/design at {head[:8]}."


def test_a_missing_task_is_named(client: TestClient) -> None:
    refusal(client.get("/api/tasks/task-999/deliverables/0"), 404, "task_not_found")
