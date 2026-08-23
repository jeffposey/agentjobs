"""Where playbooks live on disk, and reading them without running anything.

``docs/playbooks-design.md`` §3.1: playbooks are per-project repository files under a
directory named by ``playbooks_directory`` in ``.agentjobs/config.yaml``, defaulting to
``playbooks``. AgentJobs ships reference playbooks inside the package and copies them
in on request; **it never runs one implicitly**, and from the moment a copy exists the
project's copy is authoritative and tunable.

Nothing in this module executes, dispatches, or evaluates anything. Reading a playbook
reads a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .model import (
    PLAYBOOK_SUFFIX,
    Playbook,
    PlaybookError,
    PlaybookFinding,
    load_playbook,
    parse_playbook,
)

PLAYBOOKS_DIRECTORY_KEY = "playbooks_directory"
DEFAULT_PLAYBOOKS_DIRECTORY = "playbooks"

REFERENCE_PACKAGE = "agentjobs.playbooks.references"
"""Where the shipped reference playbooks live inside the installed package."""

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
"""A playbook name is a filename stem and is used to build a path, so anything that
could be read as a path component -- a separator, ``..``, a leading dot -- is refused
before it reaches the filesystem, exactly as a project id is."""


class UnknownPlaybookError(Exception):
    """Raised when a name does not resolve to a file in the playbooks directory."""


@dataclass(frozen=True)
class PlaybookListing:
    """What a directory holds: the playbooks that loaded, and the files that did not.

    Both halves, for the reason ``/tasks/broken`` exists: a file that fails validation
    and then vanishes from the listing reads as a playbook nobody ever wrote, and the
    one thing its author needs is to be told it is there and wrong.
    """

    playbooks: List[Playbook] = field(default_factory=list)
    problems: List[PlaybookFinding] = field(default_factory=list)
    directory: Optional[Path] = None
    exists: bool = True


def playbooks_directory_name(config: Optional[Dict[str, Any]]) -> str:
    """The configured directory name, or the default when nothing is configured."""
    configured = (config or {}).get(PLAYBOOKS_DIRECTORY_KEY)
    if isinstance(configured, str) and configured.strip():
        return configured
    return DEFAULT_PLAYBOOKS_DIRECTORY


def resolve_playbooks_dir(root: Path, config: Optional[Dict[str, Any]] = None) -> Path:
    """Resolve a project's playbooks directory, mirroring ``Project.tasks_dir``.

    Not created as a side effect of resolving it: a project without playbooks has no
    directory, and ``playbook list`` saying so is more useful than a silently created
    empty one appearing in ``git status``. ``playbook init`` is what creates it.
    """
    configured = Path(playbooks_directory_name(config))
    if not configured.is_absolute():
        configured = Path(root) / configured
    return configured.resolve()


def validate_playbook_name(name: str) -> str:
    """Return ``name`` if it is a legal playbook name, else raise ``ValueError``."""
    if not _NAME_PATTERN.match(name or ""):
        raise ValueError(
            f"{name!r} is not a playbook name. A name is a filename stem: letters, "
            "digits, dot, dash and underscore, starting with a letter or a digit."
        )
    return name


def list_playbooks(directory: Path) -> PlaybookListing:
    """Every ``*.md`` in ``directory``, sorted by name, with the unreadable ones beside.

    A missing directory is reported as an empty listing with ``exists=False`` rather
    than raised: no playbooks is the state every project starts in, and it is not an
    error to be in it.
    """
    resolved = Path(directory)
    if not resolved.is_dir():
        return PlaybookListing(directory=resolved, exists=False)

    playbooks: List[Playbook] = []
    problems: List[PlaybookFinding] = []
    for path in sorted(resolved.glob(f"*{PLAYBOOK_SUFFIX}")):
        if not path.is_file():
            continue
        try:
            playbooks.append(load_playbook(path))
        except PlaybookError as exc:
            problems.extend(exc.findings)
    playbooks.sort(key=lambda item: item.name)
    return PlaybookListing(playbooks=playbooks, problems=problems, directory=resolved)


def read_playbook(directory: Path, name: str) -> Playbook:
    """Load one playbook by name, or raise ``UnknownPlaybookError``."""
    validate_playbook_name(name)
    path = Path(directory) / f"{name}{PLAYBOOK_SUFFIX}"
    if not path.is_file():
        raise UnknownPlaybookError(f"No playbook named {name!r} in {directory}.")
    return load_playbook(path)


def reference_names() -> List[str]:
    """The names of the playbooks shipped inside the package, sorted."""
    root = resources.files(REFERENCE_PACKAGE)
    return sorted(
        entry.name[: -len(PLAYBOOK_SUFFIX)]
        for entry in root.iterdir()
        if entry.name.endswith(PLAYBOOK_SUFFIX)
    )


def reference_text(name: str) -> str:
    """The shipped text of one reference playbook."""
    validate_playbook_name(name)
    resource = resources.files(REFERENCE_PACKAGE).joinpath(f"{name}{PLAYBOOK_SUFFIX}")
    return resource.read_text(encoding="utf-8")


def load_reference(name: str) -> Playbook:
    """Parse a shipped reference playbook without copying it anywhere.

    The path it is given is the name it *would* have in a project, because that is
    what its findings should say if a shipped file is ever wrong -- and the suite
    checks exactly that, so a broken reference cannot ship.
    """
    return parse_playbook(Path(f"{name}{PLAYBOOK_SUFFIX}"), reference_text(name))


@dataclass(frozen=True)
class InstallResult:
    """What ``playbook init`` did: what it wrote, and what it left alone."""

    directory: Path
    written: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)

    @property
    def wrote_nothing(self) -> bool:
        """True when every shipped playbook was already present."""
        return not self.written


def install_references(directory: Path, *, names: Optional[Sequence[str]] = None) -> InstallResult:
    """Copy the shipped reference playbooks in, **never overwriting an existing file**.

    Per file rather than all-or-nothing. A project that tuned ``groom.md`` and has
    never seen ``reorder.md`` should be able to take the second without the first
    being touched or the command refusing outright -- and the tuned file is exactly
    what "never overwrite" is protecting. Both halves are reported so the caller can
    say which happened rather than implying everything was installed.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    kept: List[str] = []
    for name in names if names is not None else reference_names():
        destination = target / f"{validate_playbook_name(name)}{PLAYBOOK_SUFFIX}"
        if destination.exists():
            kept.append(name)
            continue
        destination.write_text(reference_text(name), encoding="utf-8")
        written.append(name)
    return InstallResult(directory=target, written=written, kept=kept)
