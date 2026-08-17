"""Machine-local evidence that a task file was written by the managed path.

``agentjobs validate`` can prove a task file is *valid*. It cannot prove who wrote it,
because a careful hand-edit produces a file that validates perfectly. Receipts close
that gap locally: every successful ``TaskStorage`` write records the hash of what it
just wrote, so a staged file whose hash matches no receipt was written by something
other than AgentJobs.

**This is evidence, not a security control.** The receipts are plain local files with
no secret behind them; a process that wanted to forge one could. They are aimed at the
realistic case -- an agent or a person editing YAML because it was quicker -- not at an
adversary. They are deliberately gitignored: committing them would make every write a
diff, and CI could not use them anyway, since a clean clone has none.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .__version__ import __version__

#: Where receipts live, relative to the project root. Gitignored.
RECEIPTS_DIRNAME = Path(".agentjobs") / "write-receipts"

#: Set to any non-empty value to stop writing receipts. For a machine where the
#: commit gate is not in use and the extra file per write is unwanted.
DISABLE_ENV = "AGENTJOBS_NO_RECEIPTS"


def content_hash(data: bytes) -> str:
    """The canonical hash of a persisted task file.

    Line endings are normalised first. Git may check a file out with CRLF and the
    writer may produce LF, and a hash that disagreed with itself across that would
    make the gate reject every file on Windows -- which teaches people to pass
    ``--no-verify``, which is worse than having no gate.
    """
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def hash_file(path: Path) -> Optional[str]:
    """Hash a file on disk, or None when it cannot be read."""
    try:
        return content_hash(path.read_bytes())
    except OSError:
        return None


@dataclass(frozen=True)
class Receipt:
    """One recorded managed write."""

    task_id: str
    filename: str
    content_hash: str
    operation: str
    actor: Optional[str]
    written: str
    version: str

    def to_payload(self) -> Dict[str, object]:
        """Serialise for storage."""
        return {
            "task_id": self.task_id,
            "filename": self.filename,
            "content_hash": self.content_hash,
            "operation": self.operation,
            "actor": self.actor,
            "written": self.written,
            "agentjobs_version": self.version,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "Receipt":
        """Read a stored receipt."""
        return cls(
            task_id=str(payload.get("task_id", "")),
            filename=str(payload.get("filename", "")),
            content_hash=str(payload.get("content_hash", "")),
            operation=str(payload.get("operation", "write")),
            actor=actor if isinstance(actor := payload.get("actor"), str) else None,
            written=str(payload.get("written", "")),
            version=str(payload.get("agentjobs_version", "")),
        )


class ReceiptStore:
    """Reads and writes the receipts for one project."""

    def __init__(self, directory: Path) -> None:
        """Bind the store to a project's receipts directory."""
        self.directory = Path(directory)

    @classmethod
    def for_tasks_directory(cls, tasks_dir: Path) -> "ReceiptStore":
        """Locate the receipts directory for a project's tasks directory.

        Walks up looking for the ``.agentjobs`` that marks a project root, so a
        tasks directory nested several levels down still records against its own
        project rather than creating a stray directory beside itself.
        """
        current = Path(tasks_dir).resolve()
        for candidate in [current, *current.parents]:
            if (candidate / ".agentjobs").is_dir():
                return cls(candidate / RECEIPTS_DIRNAME)
        return cls(current.parent / RECEIPTS_DIRNAME)

    @property
    def enabled(self) -> bool:
        """Whether receipts should be written on this machine."""
        return not os.environ.get(DISABLE_ENV)

    def record(
        self,
        *,
        task_id: str,
        path: Path,
        data: bytes,
        operation: str = "write",
        actor: Optional[str] = None,
    ) -> Optional[Receipt]:
        """Record one managed write.

        Never raises. A receipt is corroborating evidence; failing a task write
        because the evidence could not be filed would trade a working system for a
        record-keeping convenience.
        """
        if not self.enabled:
            return None
        receipt = Receipt(
            task_id=task_id,
            filename=Path(path).name,
            content_hash=content_hash(data),
            operation=operation,
            actor=actor,
            written=datetime.now(tz=timezone.utc).isoformat(),
            version=__version__,
        )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / f"{_safe_name(task_id)}.json"
            target.write_text(json.dumps(receipt.to_payload(), indent=2), encoding="utf-8")
        except OSError:
            return None
        return receipt

    def latest(self, task_id: str) -> Optional[Receipt]:
        """The most recent receipt for a task, or None."""
        path = self.directory / f"{_safe_name(task_id)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return Receipt.from_payload(payload)

    def matches(self, task_id: str, data: bytes) -> bool:
        """Whether this exact content was produced by a recorded managed write."""
        receipt = self.latest(task_id)
        return receipt is not None and receipt.content_hash == content_hash(data)


def _safe_name(task_id: str) -> str:
    """A filesystem-safe receipt filename for a task id."""
    return "".join(
        character if character.isalnum() or character in "-_" else "_" for character in task_id
    )
