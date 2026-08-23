"""What a run pins about the playbook it read: a name, a path, and a hash.

``docs/playbooks-design.md`` §4.2-4.3. Two rules from the design meet in this module,
and both are about the same thing -- **the brief is pointed at, never copied**:

- the prompt gains **one line** naming the playbook, and the run's task record gains a
  short pointer, because a copy forks the moment somebody fixes the playbook;
- the ``dispatch`` entry pins the file's sha256 at instantiation, because ``git_head``
  answers "which commit" and not "which brief" -- a dirty tree, a moved head, or a
  playbook edited between two runs all leave the head saying the same thing.

Kept in its own module, importing nothing from ``agentjobs.dispatch``, so the dispatch
layer can hold one of these without the two packages importing each other. The
orchestration that *composes* the two lives in :mod:`agentjobs.playbooks.run`, which is
deliberately not imported by this package's ``__init__``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from .model import Playbook

HASH_ALGORITHM = "sha256"
"""The digest the record pins. Named in the stored value, so a later change is legible."""

SHORT_DIGEST_CHARS = 12
"""How much of the digest a human-facing line shows.

Twelve hex characters is git's abbreviated-object convention and is the same order of
collision resistance: enough to identify one brief among a repository's revisions of it,
and short enough to read out. The whole digest is what the record stores -- the
abbreviation exists for the prompt line and the terminal, never for a comparison.
"""


def hash_text(text: str) -> str:
    """The sha256 of a playbook's text, hex, over UTF-8 with newlines normalised.

    Normalised because the same brief checked out on Windows and on Linux is the same
    brief, and a hash that says otherwise would report a difference no reader of the two
    files could see. ``core.autocrlf`` makes that a real difference on this machine
    rather than a hypothetical one.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlaybookPointer:
    """Which brief a run is executing, in the three facts the record needs."""

    name: str
    """The playbook's name, which is also its filename stem."""

    path: str
    """Where it was read from, relative to the project root, in posix form.

    Relative and posix because it goes into a git-tracked task record and into a prompt
    read on whichever machine dispatches next. An absolute Windows path in either place
    is a fact about one laptop.
    """

    digest: str
    """The full content hash, algorithm-prefixed: ``sha256:3f9c...``."""

    @property
    def short_digest(self) -> str:
        """The first :data:`SHORT_DIGEST_CHARS` of the hex digest, without the prefix."""
        _, _, hexdigest = self.digest.partition(":")
        return hexdigest[:SHORT_DIGEST_CHARS]

    def prompt_line(self) -> str:
        """The one line a playbook dispatch appends to the prompt stub (design §4.2).

        One line, and it is a pointer: the path to read and the hash of what was read.
        The brief itself is not here and must never be -- see the module docstring.
        """
        return (
            f"Your brief is the playbook `{self.name}` at {self.path} "
            f"({HASH_ALGORITHM} {self.short_digest}, first {SHORT_DIGEST_CHARS} of the "
            "digest). Read the task record first, then the playbook; the playbook is "
            "the specification for this run."
        )

    def spec_description(self) -> str:
        """The run task's ``spec.description``: a pointer, not a copy (design §4.2)."""
        return (
            f"Run of playbook `{self.name}`. The playbook file is the specification "
            f"for this run: read {self.path} ({HASH_ALGORITHM} {self.short_digest} at "
            "the moment this run was created). It is deliberately not copied here -- a "
            "copy would fork the brief the first time somebody improves it."
        )

    def context_reason(self) -> str:
        """Why the run task's ``context[]`` points at the playbook."""
        return (
            "The brief this run executes. It is the specification; this task record is "
            "the run's memory."
        )


def relative_path(path: Path, root: Optional[Path]) -> str:
    """``path`` as a posix path relative to ``root``, or its own posix form.

    Falls back rather than raising when the playbook is outside the project root. That
    happens only in a test or an unusual configuration, and neither is a reason to
    refuse to dispatch: an absolute path in the pointer is worse than a relative one and
    much better than a refusal nobody can act on.
    """
    resolved = Path(path)
    if root is not None:
        try:
            return PurePosixPath(resolved.resolve().relative_to(Path(root).resolve())).as_posix()
        except ValueError:
            pass
    return PurePosixPath(resolved).as_posix()


def pointer_for(playbook: "Playbook", *, project_root: Optional[Path] = None) -> PlaybookPointer:
    """Pin one playbook: its name, where it lives, and the hash of what it said.

    The hash is taken over ``playbook.source`` -- the text the parser was handed --
    rather than over a fresh read of the file. Re-reading would open a window in which
    the file changes between the parse and the hash, and the record would then pin a
    brief no run used.
    """
    return PlaybookPointer(
        name=playbook.name,
        path=relative_path(playbook.path, project_root),
        digest=f"{HASH_ALGORITHM}:{hash_text(playbook.source)}",
    )
