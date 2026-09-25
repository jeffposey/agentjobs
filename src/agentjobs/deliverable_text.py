"""Read one of a task's Markdown deliverables as it stands at the head of its branch.

A design review judges a document, not a diff, and until task-594 the owner could not
read an unmerged document from the dashboard at all -- only the handoff's summary of it.
This is the server half: given a task and the *index* of one of its ``deliverables[]``,
return that file's text at the tip of the task's one active branch.

**Security is the design constraint.** The route is reachable from a browser, and a
deliverable path is a string any actor may write onto a task, so everything below
assumes the path is hostile:

- **The caller never supplies a path.** It names an index; the path comes off the record.
- **Only a ``.md`` file**, and only a plain repository-relative one: no absolute path, no
  drive, no backslash, no ``.`` or ``..`` component, no empty component, no control
  character. Git treats an object path starting ``./`` or ``../`` as relative to the
  process's directory, so those are not cosmetic refusals.
- **The branch is resolved to a commit first**, and every object is then addressed as
  ``<40-hex sha>:<path>`` or by blob id. Neither can be read as an option, whatever the
  branch is called.
- **Only a regular blob.** A symlink is a blob too, and following one would read a file
  nobody listed; ``git ls-tree`` gives the mode that tells them apart, which
  ``git cat-file`` cannot.
- **Size is capped**, and a file over it is refused with its size, never truncated.

Nothing here checks out, writes, or runs anything but read-only git plumbing:
``rev-parse``, ``ls-tree`` and ``cat-file``.

The branch refusals use :func:`agentjobs.dispatch.finish.preflight`'s names --
``no_active_branch``, ``several_active_branches``, ``branch_missing`` -- so one condition
is called the same thing wherever the API reports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentjobs.dispatch.finish import active_branches, git, git_out
from agentjobs.models_v2 import Task

MAX_DELIVERABLE_BYTES = 512 * 1024
"""The largest document the review panel will render.

Half a megabyte of Markdown is roughly two hundred printed pages -- far past anything a
reviewer reads on a phone, and far below what makes a JSON response or a render hurt."""

REGULAR_FILE_MODES = frozenset({"100644", "100755"})
"""Git's modes for an ordinary file. ``120000`` is a symlink, ``160000`` a submodule."""

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class DeliverableRefused(Exception):
    """This deliverable cannot be shown, for a reason worth saying to the reader."""

    def __init__(self, status_code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class DeliverableText:
    """One document, and exactly where it was read from."""

    index: int
    path: str
    branch: str
    commit: str
    size: int
    text: str


def is_markdown(path: str) -> bool:
    """Whether the review panel renders this deliverable or lists it as a path."""
    return path.lower().endswith(".md")


def unsafe_path_reason(path: str) -> str | None:
    """Why ``path`` may not be handed to git, or None when it may.

    Deliberately stricter than "stays inside the repository": a deliverable is written by
    an agent, and anything unusual in one is a reason to refuse rather than to interpret.
    """
    if not path:
        return "The deliverable's path is empty."
    if _CONTROL.search(path):
        return "The deliverable's path contains a control character."
    if "\\" in path:
        return "The deliverable's path contains a backslash; write it with forward slashes."
    if path.startswith("/") or _DRIVE.match(path):
        return f"{path!r} is an absolute path; a deliverable is repository-relative."
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return (
            f"{path!r} has an empty, '.' or '..' component; a deliverable is a plain "
            "path inside the repository."
        )
    return None


def read_deliverable(root: Path, task: Task, index: int) -> DeliverableText:
    """The text of ``task.deliverables[index]`` at the head of the task's active branch.

    Raises :class:`DeliverableRefused` with a stable ``reason`` for every case the panel
    has to explain rather than render.
    """
    if index < 0 or index >= len(task.deliverables):
        raise DeliverableRefused(
            404,
            "no_such_deliverable",
            f"{task.id} lists {len(task.deliverables)} deliverable(s); there is no "
            f"deliverable {index}.",
        )
    path = task.deliverables[index].path
    unsafe = unsafe_path_reason(path)
    if unsafe:
        raise DeliverableRefused(422, "unsafe_path", unsafe)
    if not is_markdown(path):
        raise DeliverableRefused(
            422,
            "not_markdown",
            f"{path} is not a Markdown file; only .md deliverables are rendered.",
        )

    branches = active_branches(task)
    if not branches:
        raise DeliverableRefused(
            409,
            "no_active_branch",
            f"{task.id} lists no active branch, so there is nowhere to read {path} from.",
        )
    if len(branches) > 1:
        raise DeliverableRefused(
            409,
            "several_active_branches",
            f"{task.id} lists {len(branches)} active branches ({', '.join(branches)}); "
            "which one holds the document is not something to guess.",
        )
    branch = branches[0]
    commit = git_out(root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"])
    if not _SHA.match(commit):
        raise DeliverableRefused(
            404,
            "branch_missing",
            f"Branch {branch!r} does not exist in {root}. It was merged and deleted, or "
            "never created here.",
        )

    listing = git(root, ["--literal-pathspecs", "ls-tree", "-z", commit, "--", path])
    entry = _ls_tree_entry(listing.stdout or "", path) if listing.returncode == 0 else None
    if entry is None:
        raise DeliverableRefused(
            404,
            "file_missing",
            f"{path} does not exist on {branch} at {commit[:8]}.",
        )
    mode, kind, blob = entry
    if kind != "blob" or mode not in REGULAR_FILE_MODES or not _SHA.match(blob):
        raise DeliverableRefused(
            422,
            "not_a_regular_file",
            f"{path} on {branch} is not a regular file (git mode {mode}, {kind}).",
        )

    size_text = git_out(root, ["cat-file", "-s", blob])
    size = int(size_text) if size_text.isdigit() else -1
    if size < 0:
        raise DeliverableRefused(
            404, "file_missing", f"{path} on {branch} could not be read from git."
        )
    if size > MAX_DELIVERABLE_BYTES:
        raise DeliverableRefused(
            413,
            "too_large",
            f"{path} is {size:,} bytes, over the {MAX_DELIVERABLE_BYTES:,}-byte limit "
            "the review panel renders. Read it on the branch.",
        )
    content = git(root, ["cat-file", "blob", blob])
    if content.returncode != 0:
        raise DeliverableRefused(
            404, "file_missing", f"{path} on {branch} could not be read from git."
        )
    return DeliverableText(
        index=index,
        path=path,
        branch=branch,
        commit=commit[:8],
        size=size,
        text=content.stdout or "",
    )


def _ls_tree_entry(output: str, path: str) -> tuple[str, str, str] | None:
    """The ``(mode, type, object)`` of exactly ``path`` in NUL-separated ls-tree output."""
    for record in output.split("\0"):
        meta, tab, name = record.partition("\t")
        if not tab or name != path:
            continue
        fields = meta.split()
        if len(fields) == 3:
            return fields[0], fields[1], fields[2]
    return None
