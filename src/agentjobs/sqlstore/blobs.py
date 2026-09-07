"""Attachment bytes, held in the database rather than beside it.

Owner decision on task-273's second fork: blobs live in the store. That is what makes
one ``VACUUM INTO`` the whole backup, with no attachment tree to capture at the same
instant, and it removes two states the file store can reach -- a file with no entry
pointing at it, and an entry pointing at a file that is not there.

Content is addressed by its sha256, so the same screenshot attached twice is stored
once and an orphan is a refcount of zero rather than a directory walk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Set

from ..attachments import (
    MAX_ATTACHMENT_BYTES,
    MEDIA_TYPES,
    AttachmentError,
    AttachmentPayload,
    sniff_media_type,
)
from ..models_v2 import Attachment, Task
from .connection import Database


def attachment_path(task_id: str, digest: str, media_type: str) -> str:
    """The identifier an ``Attachment`` carries in ``path``.

    Kept byte-identical to what the file store produced, because it is embedded in
    every existing task record and in the URLs the React app requests. Under SQLite it
    addresses a row rather than a file, which is invisible to every reader of it.
    """
    return f"attachments/{task_id}/{digest}{MEDIA_TYPES.get(media_type, '')}"


class SqlAttachmentStore:
    """The ``AttachmentStore`` surface, backed by the ``blob`` table."""

    def __init__(self, database: Database, project_id: str) -> None:
        """Bind the store to one project inside ``database``."""
        self.database = database
        self.project_id = project_id

    def write(self, task_id: str, payload: AttachmentPayload) -> Attachment:
        """Store one image and return the record that references it.

        Validation is deliberately the same code the file store used -- the size
        ceiling, the sniffed media type, the refusal of anything that is not an image.
        Those are product rules about what an attachment *is*, and they do not change
        because the bytes moved.
        """
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
        self.put(digest, media_type, payload.data)
        return Attachment(
            path=attachment_path(task_id, digest, media_type),
            media_type=media_type,
            sha256=digest,
            size_bytes=len(payload.data),
            label=payload.label.strip() or "Attached image",
        )

    def put(self, digest: str, media_type: str, data: bytes) -> None:
        """Insert the blob if it is not already there, verifying the hash first.

        The verification is not ceremony: this is the one place bytes enter the store,
        and a content-addressed table whose addresses do not match its content is
        worse than no addressing at all.
        """
        if hashlib.sha256(data).hexdigest() != digest:
            raise AttachmentError(
                f"The bytes offered for {digest[:12]}... do not hash to it; refusing to "
                "store content under an address that does not describe it."
            )
        with self.database.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blob(sha256, media_type, size_bytes, content) "
                "VALUES (?, ?, ?, ?)",
                (digest, media_type, len(data), data),
            )

    def has(self, digest: str) -> bool:
        """Whether these bytes are already stored."""
        connection = (
            self.database.writer if self.database.in_transaction else self.database.reader()
        )
        return (
            connection.execute("SELECT 1 FROM blob WHERE sha256 = ?", (digest,)).fetchone()
            is not None
        )

    def read(self, attachment: Attachment) -> bytes:
        """The stored bytes, refused if missing or no longer matching the hash."""
        connection = (
            self.database.writer if self.database.in_transaction else self.database.reader()
        )
        row = connection.execute(
            "SELECT content FROM blob WHERE sha256 = ?", (attachment.sha256,)
        ).fetchone()
        if row is None:
            raise AttachmentError(f"The attachment {attachment.path} is not in this store.")
        data = bytes(row["content"])
        if hashlib.sha256(data).hexdigest() != attachment.sha256:  # pragma: no cover
            raise AttachmentError(
                f"The attachment {attachment.path} does not match the hash recorded for "
                "it. It has been modified or corrupted since it was stored."
            )
        return data

    def resolve(self, relative_path: str) -> Path:
        """Refused: an attachment is a row, so there is no path to resolve."""
        raise AttachmentError(
            f"{relative_path!r} has no filesystem path under SQLite storage; attachment "
            "bytes are stored in the database and read through the API."
        )

    @property
    def root(self) -> Path:
        """Refused, for the same reason as :meth:`resolve`."""
        raise AttachmentError("Attachments are rows under SQLite storage, not a directory.")

    def referenced_paths(self, tasks: List[Task]) -> Set[str]:
        """Every attachment path the given tasks point at."""
        return {
            attachment.path
            for task in tasks
            for entry in task.log
            for attachment in (entry.attachments or [])
        }

    def orphans(self, tasks: List[Task]) -> List[str]:
        """Blobs nothing references any more.

        A query over the join table rather than a directory walk, and unlike the file
        store's version it is exact: a reference can only exist as a row, so there is
        no branch or unchecked-out revision that might still point at one. Reported and
        never deleted, because the log is append-only and a blob is what an entry
        shows.
        """
        connection = (
            self.database.writer if self.database.in_transaction else self.database.reader()
        )
        return [
            row["sha256"]
            for row in connection.execute(
                "SELECT b.sha256 FROM blob b LEFT JOIN attachment a ON a.sha256 = b.sha256 "
                "WHERE a.sha256 IS NULL ORDER BY b.sha256"
            )
        ]


__all__ = ["SqlAttachmentStore", "attachment_path"]
