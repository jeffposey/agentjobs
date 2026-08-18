"""Sidecar image storage: the binary half of a text-shaped product.

AgentJobs is YAML files in git, and design doc section 7 argues the whole storage model
from that: diffable history, blame on a field, a checkout that is complete. An image has
none of those properties, so where it goes was settled as a decision before any paste
handler existed (task-067, accepted 2026-08-15): image-only sidecar files under the
tasks directory at ``attachments/<task-id>/<sha256><ext>``, with only metadata in the
YAML.

Two consequences of content-addressing are worth naming, because both are deliberate.
Pasting the same screenshot twice writes one file, since the name *is* the hash. And a
file whose bytes no longer hash to its name is refused on read rather than rendered --
the name is the integrity check, not a label beside one.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .models_v2 import Attachment, Task
from .projects import contained_path

#: Directory, relative to the tasks directory, holding every project's sidecars.
ATTACHMENTS_DIRNAME = "attachments"

#: Per-image ceiling. A screenshot is tens of kilobytes; five megabytes is a wide
#: margin around that, and the point of a ceiling is that git keeps every blob forever.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

#: Accepted image types, with the extension each is stored under. Images only: the
#: feature exists to make "look at this" immediate, and anything that cannot render
#: inline does not serve that.
MEDIA_TYPES: Dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class AttachmentError(ValueError):
    """An attachment that cannot be stored or trusted.

    Raised rather than tolerated for the same reason an unknown actor is: a record
    pointing at a file that is missing, oversized or not what it claims is worse than a
    refusal the person can act on while their prose is still in the box.
    """


@dataclass(frozen=True)
class AttachmentPayload:
    """Bytes on their way to becoming an attachment.

    The manager takes these rather than finished ``Attachment`` records, so the blob is
    written by the same call that appends the log entry referencing it. Splitting them
    would allow a file with no entry, or an entry pointing at a file that was never
    written.
    """

    data: bytes
    label: str


def sniff_media_type(data: bytes) -> Optional[str]:
    """The image type these bytes actually are, or None if not a supported image.

    Read from the content rather than taken from the caller's Content-Type or the
    filename. A declared type is a claim about a blob; the magic number is the blob.
    Trusting the claim would let a mislabelled or hostile upload be stored with an
    extension and media type that disagree with what a browser will do with it.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class AttachmentStore:
    """Reads and writes the sidecar files for one tasks directory."""

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)

    @property
    def root(self) -> Path:
        """The attachments directory. Created lazily: a project with no images has none."""
        return self.tasks_dir / ATTACHMENTS_DIRNAME

    def resolve(self, relative_path: str) -> Path:
        """Absolute path for a stored attachment, refusing anything outside the store.

        Attachment paths arrive from task files and from URL path parameters, so they
        are untrusted input in exactly the way task ids are. ``contained_path`` is the
        same guard ``_task_path`` uses.
        """
        candidate = contained_path(self.tasks_dir, relative_path)
        if not str(candidate).startswith(str(contained_path(self.tasks_dir, ATTACHMENTS_DIRNAME))):
            raise AttachmentError(f"Not an attachment path: {relative_path!r}")
        return candidate

    def write(self, task_id: str, payload: AttachmentPayload) -> Attachment:
        """Store one image beside the task and return the record that references it."""
        if not payload.data:
            raise AttachmentError("The attachment is empty.")
        if len(payload.data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                f"The image is {len(payload.data) // 1024} KiB, over the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB limit for one attachment. "
                "Crop it or save it at a lower quality, then paste again."
            )
        media_type = sniff_media_type(payload.data)
        if media_type is None:
            raise AttachmentError(
                "That is not a PNG, JPEG or WebP image. AgentJobs stores images only, "
                "so they can be shown where the entry is read."
            )

        digest = hashlib.sha256(payload.data).hexdigest()
        relative = f"{ATTACHMENTS_DIRNAME}/{task_id}/{digest}{MEDIA_TYPES[media_type]}"
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Written through a temporary file in the same directory and renamed, so a
            # reader never sees a half-written image. Content-addressed, so an existing
            # file with this name already holds exactly these bytes.
            temporary = path.with_name(f".{digest}.partial")
            temporary.write_bytes(payload.data)
            os.replace(temporary, path)
        return Attachment(
            path=relative,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(payload.data),
            label=payload.label.strip() or "Attached image",
        )

    def read(self, attachment: Attachment) -> bytes:
        """The stored bytes, refused if they are missing or no longer match the hash."""
        path = self.resolve(attachment.path)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise AttachmentError(
                f"The attachment {attachment.path} is missing from this checkout."
            ) from exc
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            raise AttachmentError(
                f"The attachment {attachment.path} does not match the hash recorded "
                "for it. It has been modified or corrupted since it was stored."
            )
        return data

    def referenced_paths(self, tasks: List[Task]) -> set[str]:
        """Every attachment path the given tasks point at."""
        return {
            attachment.path
            for task in tasks
            for entry in task.log
            for attachment in (entry.attachments or [])
        }

    def orphans(self, tasks: List[Task]) -> List[str]:
        """Stored files no task references any more, newest path order.

        Reported, never deleted. The log is append-only and a task file can be edited
        or rolled back outside this process, so a file that looks unreferenced now may
        be referenced by a revision in git history or by a branch that is not checked
        out. Deleting on that evidence would destroy the thing an entry points at.
        """
        if not self.root.exists():
            return []
        referenced = self.referenced_paths(tasks)
        found: List[str] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            relative = path.relative_to(self.tasks_dir).as_posix()
            if relative not in referenced:
                found.append(relative)
        return found
