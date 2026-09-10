"""Where a project's records live, on this machine.

There is one place task records live: a SQLite database beside the server. This file is
what says *which file* holds which project's rows, and what an import read them from.

**Machine-level, deliberately.** A project's own ``.agentjobs/config.yaml`` is versioned
and travels with a clone; the database does not. Recording a database path in a tracked
file tells a fresh clone on another machine to look in a file that machine has never had
-- an unreadable backlog with no diagnosis. So the mapping lives beside the registry in
``~/.agentjobs/storage.yaml``, which is already the place AgentJobs keeps facts that are
true of this machine and not of the repository.

The file is small on purpose::

    database: C:/Users/me/.agentjobs/agentjobs.db
    projects:
      agentjobs:
        cutover_at: '2026-09-07T05:12:00Z'
        source: C:/projects/agentjobs/tasks
        database: C:/Users/me/.agentjobs/databases/agentjobs.db

``source`` and ``cutover_at`` are kept after an import so an operator reading this file
can see what a project's records were brought in from, without consulting a task record.
Both are absent on a project that was created on the database and never held files.

**A project's ``database`` is its own file, and the top-level key is only a fallback.**
One file for every project makes any decision about one project's records a decision
about all of them -- publishing a backlog, backing one up on its own schedule, handing
one to somebody else, deleting one. So :func:`default_project_database` names a file per
project under ``~/.agentjobs/databases/``, an import writes that path into the entry, and
sharing a file is something an operator asks for rather than what happens by accident
(task-400). The top-level ``database:`` still answers for a project whose entry names no
file, which is what keeps a machine configured before that change working untouched.

**An entry saying ``backend: files`` is refused, by name** (task-402). The file backend
is gone, so such a project has an unreadable backlog rather than a stale one, and every
command must say so rather than silently answering from an empty database. The repair is
``agentjobs storage import``, which the error names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .projects import default_home

STORAGE_FILENAME = "storage.yaml"
"""Name of the machine-level storage configuration, beside ``projects.yaml``."""

DATABASE_ENV = "AGENTJOBS_DATABASE"
"""Redirects **every** project on this machine to one file, whatever their entries say.

Deliberately still machine-wide after task-400 gave each project its own database. It is
the one-invocation escape hatch -- a test harness, an operator pointing the whole machine
at a copy on another volume -- and a per-project form would have to be a variable per
project or a parsed map, whose failure mode is that you set it and the one project you
forgot silently kept writing somewhere else. That is the exact silent divergence
per-project files exist to remove. An operator who wants *one* project elsewhere edits
its ``database`` in ``storage.yaml``, which is durable, readable and reviewable.
"""

DEFAULT_DATABASE_NAME = "agentjobs.db"

DATABASES_DIRNAME = "databases"
"""Directory beside the registry that holds one database file per project."""

FILES = "files"
"""The retired backend, kept only so an entry naming it can be refused by name."""


class StorageConfigError(RuntimeError):
    """The storage configuration could not be read, or asks for something impossible."""


@dataclass(frozen=True)
class ProjectStorage:
    """What one project's entry says.

    ``source`` and ``cutover_at`` are absent on a project created on the database, which
    is every project since task-399 and every new one from here.

    ``database`` is absent on a project imported before task-400, and on one an operator
    deliberately kept in the machine's shared file. Both fall back to the top-level
    ``database:`` key, which is what makes this field an addition rather than a
    requirement.

    There is no ``backend`` field. A project entry names a database, not a choice of
    backend -- the choice went with the file backend (task-402).
    """

    cutover_at: Optional[str] = None
    source: Optional[str] = None
    database: Optional[str] = None

    def as_dict(self) -> Dict[str, str]:
        """The mapping this entry serialises to, omitting what was never set."""
        payload: Dict[str, str] = {}
        if self.cutover_at:
            payload["cutover_at"] = self.cutover_at
        if self.source:
            payload["source"] = self.source
        if self.database:
            payload["database"] = self.database
        return payload


@dataclass(frozen=True)
class StorageSettings:
    """The whole file, read once.

    Immutable, and every mutator returns a new one. Storage configuration is read on
    nearly every code path and written on two; making the common case a value that
    cannot change under a caller is worth the copy on the rare one.
    """

    home: Path
    database: Path
    """The machine's fallback file, for a project whose entry names no database.

    Not "the database" any more. Ask :meth:`database_for` instead unless you genuinely
    mean the fallback -- a caller that opens this with a project in hand is reading the
    wrong file for any project that has been given its own (task-400).
    """

    projects: Dict[str, ProjectStorage]

    @property
    def path(self) -> Path:
        """Where this configuration is read from and written to."""
        return self.home / STORAGE_FILENAME

    def for_project(self, project_id: str) -> ProjectStorage:
        """This project's entry, or the default one. Never raises for an unknown id.

        A project with no entry is not an error: it is one nobody has recorded a
        database for yet, and :meth:`database_for` gives it the per-project default.
        Raising here would make "where are this project's records" a question that can
        fail, and it is asked from inside request handling.
        """
        return self.projects.get(project_id, ProjectStorage())

    def database_for(self, project_id: str) -> Path:
        """The file this project's rows live in. The whole precedence, in one place.

        The environment wins over everything, then the project's own entry, then --
        depending on whether the project has an entry at all -- the machine's shared
        file or a default of its own. Reading the variable here rather than trusting
        :attr:`database` is what makes the override still mean *every* project after
        task-400: a project that named its own file would otherwise quietly ignore it.

        **The two fallbacks are different answers to different questions.** An entry
        that names no database is an operator saying "share the machine's file", which
        is the shape every cutover had before task-400. *No entry* says nothing at all,
        and the default for a project nobody has recorded is a file of its own -- the
        same one ``init`` would have given it.
        """
        override = os.environ.get(DATABASE_ENV)
        if override:
            return Path(override).expanduser().resolve()
        entry = self.projects.get(project_id)
        if entry is None:
            return default_project_database(project_id, self.home)
        if entry.database:
            return Path(entry.database).expanduser().resolve()
        return self.database

    def databases(self, project_ids: Optional[List[str]] = None) -> List[Path]:
        """Every distinct file these projects' records are in, in first-seen order.

        What ``storage backup`` iterates. Distinct rather than one per project, because
        two projects sharing a file must not produce two snapshots of it -- the second
        would refuse, backups never being overwritten.
        """
        ids = list(self.projects) if project_ids is None else list(project_ids)
        found: List[Path] = []
        for project_id in ids:
            resolved = self.database_for(project_id)
            if resolved not in found:
                found.append(resolved)
        return found

    # ----- mutation ---------------------------------------------------------

    def with_project(self, project_id: str, entry: ProjectStorage) -> "StorageSettings":
        """A copy carrying one changed project entry."""
        projects = dict(self.projects)
        projects[project_id] = entry
        return replace(self, projects=projects)

    def with_database(self, database: Path) -> "StorageSettings":
        """A copy pointing at another database file."""
        return replace(self, database=Path(database))

    def save(self) -> Path:
        """Write this configuration, creating the home directory if needed."""
        self.home.mkdir(parents=True, exist_ok=True)
        payload = {
            "database": str(self.database),
            "projects": {
                project_id: entry.as_dict() for project_id, entry in sorted(self.projects.items())
            },
        }
        self.path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )
        return self.path


def default_database(home: Optional[Path] = None) -> Path:
    """Where the store lives when nothing says otherwise.

    Beside the registry, which is outside every code worktree by construction. That is
    the whole requirement the spec states for this path: a database inside a checkout
    would be branch-switched, rebased over and gitignored back into the exact coupling
    this migration removes.
    """
    override = os.environ.get(DATABASE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path(home) if home else default_home()) / DEFAULT_DATABASE_NAME


def default_project_database(project_id: str, home: Optional[Path] = None) -> Path:
    """Where a project's records go when nobody names a file: one of its own.

    Named for the project id, in ``databases/`` beside the registry -- outside every
    code worktree, for the same reason :func:`default_database` is. The id is already
    constrained to a filename-safe shape by the registry, so it is used as the stem
    without mangling: a path an operator cannot map back to a project by looking at it
    is a path they will not maintain.
    """
    override = os.environ.get(DATABASE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    root = Path(home) if home else default_home()
    return root / DATABASES_DIRNAME / f"{project_id}.db"


def load_storage_settings(home: Optional[Path] = None) -> StorageSettings:
    """Read the machine's storage configuration, defaulting everything that is absent.

    A missing file is not an error and never will be: it is what every machine looks
    like before a cutover, and what one looks like again after a rollback that removed
    the last entry.
    """
    resolved_home = Path(home).expanduser().resolve() if home else default_home()
    path = resolved_home / STORAGE_FILENAME
    database = default_database(resolved_home)
    if not path.is_file():
        return StorageSettings(home=resolved_home, database=database, projects={})

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StorageConfigError(f"Cannot read storage configuration at {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise StorageConfigError(f"Invalid storage configuration at {path}: expected a mapping.")

    # The environment override wins over the file, so a test or an operator moving the
    # store for one invocation does not have to edit -- and then remember to restore --
    # the machine's configuration.
    if not os.environ.get(DATABASE_ENV):
        configured = loaded.get("database")
        if configured is not None:
            if not isinstance(configured, str):
                raise StorageConfigError(
                    f"Invalid storage configuration at {path}: 'database' must be a string."
                )
            database = Path(configured).expanduser().resolve()

    raw_projects = loaded.get("projects") or {}
    if not isinstance(raw_projects, dict):
        raise StorageConfigError(
            f"Invalid storage configuration at {path}: 'projects' must be a mapping."
        )

    projects: Dict[str, ProjectStorage] = {}
    stranded: List[str] = []
    for project_id, entry in raw_projects.items():
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise StorageConfigError(
                f"Invalid storage configuration at {path}: entry for {project_id!r} "
                "must be a mapping."
            )
        # Refused by name rather than ignored. A project still recorded on the file
        # backend has an *unreadable* backlog after task-402, not a stale one, and
        # answering from an empty database would look like an empty project.
        if entry.get("backend") == FILES:
            stranded.append(str(project_id))
        configured_database = entry.get("database")
        if configured_database is not None and not isinstance(configured_database, str):
            raise StorageConfigError(
                f"Invalid storage configuration at {path}: 'database' for "
                f"{project_id!r} must be a string."
            )
        projects[str(project_id)] = ProjectStorage(
            cutover_at=entry.get("cutover_at"),
            source=entry.get("source"),
            database=configured_database,
        )

    if stranded:
        raise StorageConfigError(
            f"{path} still records {', '.join(sorted(stranded))} on the retired file "
            "backend. Task records are rows now, so that project has no readable "
            "backlog rather than a stale one. Bring its task YAML in with "
            "`agentjobs storage import <project> --source <directory>`, which writes "
            "the entry this file needs."
        )

    return StorageSettings(home=resolved_home, database=database, projects=projects)


def record_cutover(
    project_id: str,
    source: Path,
    *,
    home: Optional[Path] = None,
    database: Optional[Path] = None,
    shared: bool = False,
) -> StorageSettings:
    """Mark one project as served from the database, and say what it was migrated from.

    Written *after* the import has been verified, never before: this file is what every
    client reads to decide where to look, so flipping it early points the whole machine
    at a store that does not yet hold the corpus.

    ``database`` names the file; with neither it nor ``shared`` the entry gets the
    per-project default. ``shared=True`` writes no path, which is how an operator opts
    a project into the machine's one file -- the shape every cutover had before
    task-400, and now a thing somebody asks for.
    """
    settings = load_storage_settings(home)
    if shared:
        resolved_database: Optional[str] = None
    elif database is not None:
        resolved_database = str(Path(database).expanduser().resolve())
    else:
        resolved_database = str(default_project_database(project_id, settings.home))
    entry = ProjectStorage(
        cutover_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source=str(Path(source).resolve()),
        database=resolved_database,
    )
    updated = settings.with_project(project_id, entry)
    updated.save()
    return updated


def record_new_project(
    project_id: str,
    *,
    home: Optional[Path] = None,
    database: Optional[Path] = None,
) -> StorageSettings:
    """Put a project that has never held task files on the database from the start.

    What ``agentjobs init`` writes (task-399), and separate from :func:`record_cutover`
    because there is nothing to cut over. The entry carries no ``source`` and no
    ``cutover_at``: both would be claims about a migration that did not happen, and
    A project initialised on the database has no source directory, and saying so by
    omission is more honest than naming one that was never authoritative.

    An entry that already exists is left exactly as it is and returned unchanged. This
    is reached by ``init`` on a directory whose project id is already configured, and
    overwriting there would silently discard a recorded import -- including the
    ``database`` its rows are actually in.
    """
    settings = load_storage_settings(home)
    if project_id in settings.projects:
        return settings
    resolved_database = (
        str(Path(database).expanduser().resolve())
        if database is not None
        else str(default_project_database(project_id, settings.home))
    )
    updated = settings.with_project(project_id, ProjectStorage(database=resolved_database))
    updated.save()
    return updated


def record_database(
    project_id: str, database: Path, *, home: Optional[Path] = None
) -> StorageSettings:
    """Point one project at a file of its own, leaving the rest of its entry alone.

    What ``storage split`` writes once the copy verifies, and the reason it is separate
    from :func:`record_cutover` is that a split is not an import: the source directory
    and the timestamp are both still true of the project, and rewriting them would lose
    when its records actually arrived.
    """
    settings = load_storage_settings(home)
    entry = settings.for_project(project_id)
    updated = settings.with_project(
        project_id,
        replace(entry, database=str(Path(database).expanduser().resolve())),
    )
    updated.save()
    return updated


__all__ = [
    "DATABASES_DIRNAME",
    "DATABASE_ENV",
    "DEFAULT_DATABASE_NAME",
    "FILES",
    "STORAGE_FILENAME",
    "ProjectStorage",
    "StorageConfigError",
    "StorageSettings",
    "default_database",
    "default_project_database",
    "load_storage_settings",
    "record_cutover",
    "record_database",
    "record_new_project",
]
