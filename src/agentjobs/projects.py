"""Machine-level registry of AgentJobs projects.

AgentJobs was built single-project: the server resolved its configuration from the
process working directory, so serving a second project meant running a second server.
This module is the seam that removes that assumption. It maps a short project id to a
project root on this machine, and it is the *only* place an id becomes a path.

The registry deliberately holds nothing but the mapping. Each project continues to own
its ``.agentjobs/config.yaml`` -- categories, agents, tasks_directory -- because that
config is versioned with the project and travels when the repo is cloned. The registry
is machine-local and disposable: a list of what this particular machine has checked out.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

HOME_ENV = "AGENTJOBS_HOME"
"""Overrides the registry location. Primarily so tests never touch a real home dir."""

CONFIG_RELATIVE = Path(".agentjobs") / "config.yaml"
REGISTRY_FILENAME = "projects.yaml"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
"""Project ids are lowercase slugs. Anything that could be read as a path component
navigation token -- ``.``, ``..``, anything containing a separator -- is rejected before
it can reach the filesystem."""


class ProjectError(Exception):
    """Raised when a project cannot be registered, resolved, or trusted."""


class UnknownProjectError(ProjectError):
    """Raised when a project id is not present in the registry."""


class AmbiguousProjectError(ProjectError):
    """Raised when no default project can be resolved without guessing."""


@dataclass(frozen=True)
class Project:
    """A registered project: an id, a display name, and a root directory."""

    id: str
    name: str
    root: Path

    @property
    def config_path(self) -> Path:
        """Path to this project's own AgentJobs configuration."""
        return self.root / CONFIG_RELATIVE

    def load_config(self) -> dict:
        """Read this project's configuration, or an empty mapping if it has none."""
        return load_project_config(self.root)

    def tasks_dir(self) -> Path:
        """Resolve this project's tasks directory from its own config."""
        configured = self.load_config().get("tasks_directory") or "tasks"
        tasks_dir = Path(configured)
        if not tasks_dir.is_absolute():
            tasks_dir = self.root / tasks_dir
        return tasks_dir.resolve()

    def webhooks_path(self) -> Path:
        """Resolve this project's webhook store."""
        return self.root / ".agentjobs" / "webhooks.yaml"


def default_home() -> Path:
    """Directory holding the machine-level registry."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".agentjobs"


def load_project_config(root: Path, *, required: bool = False) -> dict:
    """Load and minimally validate a project's AgentJobs configuration."""
    resolved_root = Path(root).expanduser().resolve()
    config_path = resolved_root / CONFIG_RELATIVE
    if not config_path.is_file():
        if required:
            raise ProjectError(
                f"No AgentJobs config found at {config_path}. Initialize the project first."
            )
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProjectError(f"Cannot read AgentJobs config at {config_path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ProjectError(f"Invalid AgentJobs config at {config_path}: expected a mapping.")
    for field in ("project_name", "tasks_directory"):
        value = loaded.get(field)
        if value is not None and not isinstance(value, str):
            raise ProjectError(
                f"Invalid AgentJobs config at {config_path}: {field} must be a string."
            )
    return loaded


def slugify_project_id(value: str) -> str:
    """Derive a valid project id from arbitrary text, usually a directory name."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    if not slug:
        raise ProjectError(f"Cannot derive a valid project id from {value!r}")
    return validate_project_id(slug)


def validate_project_id(value: str) -> str:
    """Return a valid lowercase project id or raise a usable input error."""
    if not _ID_PATTERN.match(value):
        raise ProjectError(
            f"Invalid project id {value!r}: use lowercase letters, digits, '.', '-' or '_', "
            "starting with a letter or digit."
        )
    return value


class ProjectRegistry:
    """Reads and writes the machine-level project registry.

    Every id-to-path resolution in AgentJobs goes through this class, which is what
    makes path containment enforceable in one place rather than at each call site.
    """

    def __init__(self, home: Optional[Path] = None):
        """Initialize the registry, defaulting to the machine-level location."""
        self.home = Path(home).expanduser().resolve() if home else default_home()
        self.path = self.home / REGISTRY_FILENAME

    # ----- persistence -------------------------------------------------------

    def _read(self) -> List[dict]:
        """Read raw registry entries from disk."""
        if not self.path.exists():
            return []
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        entries = data.get("projects") or []
        if not isinstance(entries, list):
            raise ProjectError(f"Malformed registry at {self.path}: 'projects' must be a list")
        return entries

    def _write(self, entries: List[dict]) -> None:
        """Persist registry entries to disk."""
        self.home.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump({"projects": entries}, sort_keys=False, allow_unicode=False)
        self.path.write_text(payload, encoding="utf-8")

    # ----- queries -----------------------------------------------------------

    def list_projects(self) -> List[Project]:
        """Every registered project, ordered by id."""
        projects = [
            Project(
                id=entry["id"],
                name=entry.get("name") or entry["id"],
                root=Path(entry["root"]),
            )
            for entry in self._read()
            if entry.get("id") and entry.get("root")
        ]
        return sorted(projects, key=lambda project: project.id)

    def as_dict(self) -> Dict[str, Project]:
        """Registered projects keyed by id."""
        return {project.id: project for project in self.list_projects()}

    def get(self, project_id: str) -> Project:
        """Resolve a project id, or raise if it is unknown.

        Lookup is exact-match against registered ids. It is never a path join, so a
        crafted id cannot reach a directory that was not deliberately registered.
        """
        try:
            return self.as_dict()[project_id]
        except KeyError:
            known = ", ".join(sorted(self.as_dict())) or "none registered"
            raise UnknownProjectError(
                f"Unknown project {project_id!r}. Registered projects: {known}."
            ) from None

    def resolve_default(self, cwd: Optional[Path] = None) -> Project:
        """Resolve the project that unscoped requests act on.

        In order: the registered project containing ``cwd``, then the sole registered
        project if there is exactly one. With several projects and no positional clue we
        raise rather than guess -- silently acting on the wrong project is worse than an
        error naming the ambiguity.
        """
        projects = self.list_projects()
        if not projects:
            raise AmbiguousProjectError(
                "No projects are registered. Run 'agentjobs project add <path>' "
                "or 'agentjobs init' in a project directory."
            )

        here = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        containing = [
            project for project in projects if _is_within(here, _safe_resolve(project.root))
        ]
        if containing:
            # Deepest root wins, so a project nested inside another resolves to itself.
            return max(containing, key=lambda project: len(str(_safe_resolve(project.root))))

        if len(projects) == 1:
            return projects[0]

        known = ", ".join(project.id for project in projects)
        raise AmbiguousProjectError(
            f"Cannot resolve a default project from {here}: it is not inside any "
            f"registered project, and {len(projects)} are registered ({known}). "
            "Address a project explicitly, e.g. /api/projects/<id>/tasks."
        )

    # ----- mutation ----------------------------------------------------------

    def add(
        self,
        root: Path,
        project_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Project:
        """Register a project directory, returning the stored entry."""
        resolved_root = Path(root).expanduser().resolve()
        if not resolved_root.is_dir():
            raise ProjectError(f"Not a directory: {resolved_root}")

        config = load_project_config(resolved_root)

        identifier = project_id or slugify_project_id(
            config.get("project_name") or resolved_root.name
        )
        validate_project_id(identifier)

        entries = [entry for entry in self._read() if entry.get("id") != identifier]
        for entry in entries:
            if Path(entry["root"]) == resolved_root:
                raise ProjectError(f"{resolved_root} is already registered as {entry['id']!r}.")

        entries.append(
            {
                "id": identifier,
                "name": name or config.get("project_name") or resolved_root.name,
                "root": str(resolved_root),
            }
        )
        self._write(entries)
        return self.get(identifier)

    def remove(self, project_id: str) -> None:
        """Unregister a project. The project's own files are never touched."""
        entries = self._read()
        remaining = [entry for entry in entries if entry.get("id") != project_id]
        if len(remaining) == len(entries):
            raise UnknownProjectError(f"Unknown project {project_id!r}.")
        self._write(remaining)


def _safe_resolve(path: Path) -> Path:
    """Resolve a path without requiring it to exist."""
    return Path(path).expanduser().resolve()


def _is_within(candidate: Path, parent: Path) -> bool:
    """True when ``candidate`` is ``parent`` or lives beneath it."""
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def contained_path(base_dir: Path, filename: str) -> Path:
    """Join ``filename`` onto ``base_dir``, refusing anything that escapes it.

    ``Path`` joining is not safe on untrusted input: ``base / "../../etc/passwd"``
    happily produces a path outside ``base``, and on Windows ``base / "C:/Windows"``
    discards ``base`` entirely. This resolves the composed path and verifies the result
    still lives under ``base_dir``.
    """
    base = _safe_resolve(base_dir)
    candidate = _safe_resolve(base / filename)
    if not _is_within(candidate, base):
        raise ProjectError(f"Refusing path outside the project directory: {filename!r}")
    return candidate
