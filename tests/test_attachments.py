"""Sidecar image storage, and the two surfaces that write it.

The point of these tests is the property the storage decision was made to protect: a
task file stays something a person can read in a text editor and git can diff, while
the image is a real file beside it.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentPayload,
    AttachmentStore,
    sniff_media_type,
)
from agentjobs.manager import TaskManager
from agentjobs.storage import TaskStorage


def png_bytes(payload: bytes = b"agentjobs") -> bytes:
    """A structurally valid single-pixel PNG, with `payload` riding in a text chunk.

    Real bytes rather than a stub, because the store reads the type from the magic
    number: a fake would test the test. The payload lets one test produce two images
    that differ, so deduplication can be told apart from overwriting.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"tEXt", b"Comment\x00" + payload)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
    )


def as_upload(data: bytes, label: str = "Screenshot") -> dict:
    return {"data_base64": base64.b64encode(data).decode("ascii"), "label": label}


# ----- the store ---------------------------------------------------------------


def test_sniffing_reads_the_bytes_rather_than_a_claim() -> None:
    assert sniff_media_type(png_bytes()) == "image/png"
    assert sniff_media_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"
    # A PDF renamed to .png is still a PDF, and would render as a broken image.
    assert sniff_media_type(b"%PDF-1.7\n") is None


def test_writing_an_image_stores_it_beside_the_tasks(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    data = png_bytes()
    attachment = store.write("task-001-example", AttachmentPayload(data=data, label="  Before  "))

    assert attachment.path.startswith("attachments/task-001-example/")
    assert attachment.path.endswith(".png")
    assert attachment.media_type == "image/png"
    assert attachment.size_bytes == len(data)
    assert attachment.label == "Before"
    assert (tmp_path / attachment.path).read_bytes() == data
    assert store.read(attachment) == data


def test_the_same_image_twice_is_stored_once(tmp_path: Path) -> None:
    """Content-addressing, stated as a behaviour rather than left as a side effect."""
    store = AttachmentStore(tmp_path)
    first = store.write("task-001", AttachmentPayload(data=png_bytes(), label="a"))
    second = store.write("task-001", AttachmentPayload(data=png_bytes(), label="b"))
    other = store.write("task-001", AttachmentPayload(data=png_bytes(b"different"), label="c"))

    assert first.path == second.path
    assert other.path != first.path
    stored = list((tmp_path / "attachments" / "task-001").glob("*.png"))
    assert len(stored) == 2


def test_a_modified_file_is_refused_rather_than_served(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    attachment = store.write("task-001", AttachmentPayload(data=png_bytes(), label="x"))
    (tmp_path / attachment.path).write_bytes(png_bytes(b"tampered"))

    with pytest.raises(AttachmentError, match="does not match the hash"):
        store.read(attachment)


def test_a_missing_file_says_so_instead_of_raising_oserror(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    attachment = store.write("task-001", AttachmentPayload(data=png_bytes(), label="x"))
    (tmp_path / attachment.path).unlink()

    with pytest.raises(AttachmentError, match="missing from this checkout"):
        store.read(attachment)


def test_a_path_escaping_the_store_is_refused(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    for hostile in ("../../secrets.png", "attachments/../../secrets.png", "task-001.yaml"):
        with pytest.raises(Exception):
            store.resolve(hostile)


def test_a_non_image_and_an_oversized_image_are_both_refused(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentError, match="not a PNG, JPEG or WebP"):
        store.write("task-001", AttachmentPayload(data=b"%PDF-1.7\n", label="x"))
    with pytest.raises(AttachmentError, match="over the 5 MiB limit"):
        store.write(
            "task-001",
            AttachmentPayload(data=b"\x89PNG\r\n\x1a\n" + b"0" * MAX_ATTACHMENT_BYTES, label="x"),
        )
    assert not (tmp_path / "attachments").exists() or not list(
        (tmp_path / "attachments").rglob("*.png")
    )


def test_orphans_are_reported_and_never_deleted(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    manager = TaskManager(storage)
    task = manager.create_task(
        title="Has an image",
        description="body",
        actor="claude",
        attachments=[AttachmentPayload(data=png_bytes(), label="kept")],
    )
    referenced = task.log[0].attachments[0].path  # type: ignore[index]
    stray = tmp_path / "attachments" / task.id / "deadbeef.png"
    stray.write_bytes(png_bytes(b"stray"))

    orphans = storage.attachments.orphans(storage.list_tasks())
    assert orphans == [f"attachments/{task.id}/deadbeef.png"]
    assert referenced not in orphans
    # Reported, not removed: the file is still there afterwards.
    assert stray.exists()


def test_the_cli_reports_orphans_without_removing_them(tmp_path: Path, monkeypatch) -> None:
    """The maintenance surface the storage decision asked for: report, never delete."""
    from typer.testing import CliRunner

    from agentjobs.cli import app as cli_app

    # The project fixture already made this; the CLI reads the same directory.
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    storage = TaskStorage(tasks_dir)
    manager = TaskManager(storage)
    task = manager.create_task(
        title="Has an image",
        description="body",
        actor="claude",
        attachments=[AttachmentPayload(data=png_bytes(), label="kept")],
    )
    stray = tasks_dir / "attachments" / task.id / "deadbeef.png"
    stray.write_bytes(png_bytes(b"stray"))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    result = CliRunner().invoke(cli_app, ["attachments", "--orphans"])

    assert result.exit_code == 0, result.output
    assert "deadbeef.png" in result.output
    assert "Nothing was deleted" in result.output
    assert stray.exists()


# ----- the record --------------------------------------------------------------


def test_the_task_file_stays_readable_and_carries_only_metadata(tmp_path: Path) -> None:
    """ac-5, asserted on the bytes on disk rather than on the model.

    This is the whole storage decision in one test: if a screenshot ever ends up inline
    the file stops being diffable, and that is the property the YAML model exists for.
    """
    storage = TaskStorage(tmp_path)
    manager = TaskManager(storage)
    task = manager.create_task(
        title="Filters match nothing",
        description="Every filter returns zero rows.",
        actor="claude",
        attachments=[AttachmentPayload(data=png_bytes(), label="The empty list")],
    )

    text = (tmp_path / f"{task.id}.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    entry = document["log"][0]
    assert entry["attachments"] == [
        {
            "path": entry["attachments"][0]["path"],
            "media_type": "image/png",
            "sha256": entry["attachments"][0]["sha256"],
            "size_bytes": len(png_bytes()),
            "label": "The empty list",
        }
    ]
    # No base64 blob anywhere, and every line still short enough to read.
    assert "data:image" not in text
    assert max(len(line) for line in text.splitlines()) < 200


def test_an_entry_without_images_gains_no_attachments_key(tmp_path: Path) -> None:
    """Additive means additive: existing files must not all grow a field they never use."""
    storage = TaskStorage(tmp_path)
    manager = TaskManager(storage)
    task = manager.create_task(title="Plain", description="No images.", actor="claude")

    text = (tmp_path / f"{task.id}.yaml").read_text(encoding="utf-8")
    assert "attachments" not in text


# ----- the two surfaces ---------------------------------------------------------


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
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
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tasks_dir))
    reset_dependency_cache()
    yield
    reset_dependency_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_a_reported_issue_carries_its_screenshot(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={
            "title": "Filters match nothing",
            "description": "Every filter returns zero rows.",
            "actor": "Jeff Posey",
            "tags": ["reported-issue"],
            "attachments": [as_upload(png_bytes(), "The empty list")],
        },
    )
    assert response.status_code == 201
    task = response.json()
    attachment = task["log"][0]["attachments"][0]
    assert attachment["media_type"] == "image/png"
    assert attachment["label"] == "The empty list"

    served = client.get(f"/api/tasks/{task['id']}/attachments/{attachment['path'].split('/')[-1]}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.content == png_bytes()


def test_requested_changes_carry_a_screenshot_of_what_is_wrong(client: TestClient) -> None:
    created = client.post(
        "/api/tasks",
        json={"title": "Under review", "description": "spec", "lifecycle": "ready"},
    ).json()
    task_id = created["id"]
    client.post(f"/api/tasks/{task_id}/claim", json={"agent": "claude"})
    client.post(
        f"/api/tasks/{task_id}/handoff",
        json={"agent": "claude", "ball": "human", "ball_reason": "review", "ball_prompt": "Look."},
    )

    response = client.post(
        f"/api/tasks/{task_id}/request-changes",
        json={
            "user": "Jeff Posey",
            "feedback": "The badge shows the enum name.",
            "attachments": [as_upload(png_bytes(), "The badge")],
        },
    )
    assert response.status_code == 200
    entry = response.json()["task"]["log"][-1]
    assert entry["type"] == "handoff"
    assert entry["actor"] == "Jeff Posey"
    assert entry["attachments"][0]["label"] == "The badge"


def test_an_oversized_paste_is_refused_and_writes_nothing(client: TestClient) -> None:
    """ac-6 at the API: the refusal names the problem and the task is not created."""
    response = client.post(
        "/api/tasks",
        json={
            "title": "Too big",
            "description": "prose the reporter typed",
            "actor": "Jeff Posey",
            "attachments": [as_upload(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_ATTACHMENT_BYTES)],
        },
    )
    assert response.status_code == 400
    assert "over the 5 MiB limit" in response.text
    assert client.get("/api/tasks").json() == []


def test_a_non_image_upload_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={
            "title": "Not an image",
            "description": "prose",
            "actor": "Jeff Posey",
            "attachments": [as_upload(b"%PDF-1.7\nnot an image")],
        },
    )
    assert response.status_code == 400
    assert "PNG, JPEG or WebP" in response.text


def test_undecodable_base64_is_refused_by_index(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={
            "title": "Broken payload",
            "description": "prose",
            "actor": "Jeff Posey",
            "attachments": [as_upload(png_bytes()), {"data_base64": "not base64!", "label": "x"}],
        },
    )
    assert response.status_code == 400
    assert "Attachment 2" in response.text


def test_serving_refuses_a_file_no_entry_references(client: TestClient) -> None:
    """The route resolves through the record, so it cannot be turned into a file server."""
    created = client.post(
        "/api/tasks",
        json={
            "title": "Has one image",
            "description": "prose",
            "actor": "Jeff Posey",
            "attachments": [as_upload(png_bytes())],
        },
    ).json()

    assert client.get(f"/api/tasks/{created['id']}/attachments/nothing.png").status_code == 404
    assert client.get(
        f"/api/tasks/{created['id']}/attachments/..%2F..%2Fconfig.yaml"
    ).status_code in (
        404,
        400,
    )
