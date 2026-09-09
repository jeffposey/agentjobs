"""Which backend holds a project's tasks, and where that database lives.

This is the file the cutover writes. Everything else about task-311 -- the import, the
CLI going over HTTP, the retirement of the task-file Git workflows -- is downstream of
one question: *for this project, on this machine, is the authority YAML files in the
checkout or the SQLite database beside the server?*

**Machine-level, deliberately.** A project's own ``.agentjobs/config.yaml`` is versioned
and travels with a clone; the database does not. Recording "this project is on SQLite"
in a tracked file tells a fresh clone on another machine that its tasks live in a
database that machine has never had -- an unreadable backlog with no diagnosis. So the
mapping lives beside the registry in ``~/.agentjobs/storage.yaml``, which is already the
place AgentJobs keeps facts that are true of this machine and not of the repository.

The file is small on purpose::

    database: C:/Users/me/.agentjobs/agentjobs.db
    projects:
      agentjobs:
        backend: sqlite
        cutover_at: '2026-09-07T05:12:00Z'
        source: C:/projects/agentjobs/tasks
        database: C:/Users/me/.agentjobs/databases/agentjobs.db

``source`` is kept after the cutover because rollback needs to know which directory the
records came from, and because an operator reading this file should be able to see what
was replaced without consulting a task record.

**A project's ``database`` is its own file, and the top-level key is only a fallback.**
One file for every project makes any decision about one project's records a decision
about all of them -- publishing a backlog, backing one up on its own schedule, handing
one to somebody else, deleting one. So :func:`default_project_database` names a file per
project under ``~/.agentjobs/databases/``, a cutover writes that path into the entry, and
sharing a file is something an operator asks for rather than what happens by accident
(task-400). The top-level ``database:`` still answers for a project whose entry does not
name one, which is what keeps a machine configured before this change working untouched.

**The default is ``files``.** A machine that has never run a cutover has no such file,
every project resolves to the file backend, and nothing about this module is reachable.
That is what makes the cutover a step somebody takes rather than an upgrade that happens
to them.
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
SQLITE = "sqlite"
BACKENDS = (FILES, SQLITE)


class StorageConfigError(RuntimeError):
    """The storage configuration could not be read, or asks for something impossible."""


@dataclass(frozen=True)
class ProjectStorage:
    """What one project's entry says.

    ``source`` and ``cutover_at`` are absent on a project that has never been cut over,
    which is every project until an operator runs ``agentjobs storage cutover``.

    ``database`` is absent on a project cut over before task-400, and on one an operator
    deliberately kept in the machine's shared file. Both fall back to the top-level
    ``database:`` key, which is what makes this field an addition rather than a
    requirement.
    """

    backend: str = FILES
    cutover_at: Optional[str] = None
    source: Optional[str] = None
    database: Optional[str] = None

    @property
    def on_sqlite(self) -> bool:
        """True when this project's authority is the database."""
        return self.backend == SQLITE

    def as_dict(self) -> Dict[str, str]:
        """The mapping this entry serialises to, omitting what was never set."""
        payload: Dict[str, str] = {"backend": self.backend}
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

        An unregistered project is on files, because everything is on files until
        somebody cuts it over. Raising here would make "have we migrated yet" a question
        that can fail, and it is asked from inside request handling.
        """
        return self.projects.get(project_id, ProjectStorage())

    def backend_for(self, project_id: str) -> str:
        """``"files"`` or ``"sqlite"`` for one project."""
        return self.for_project(project_id).backend

    def on_sqlite(self, project_id: str) -> bool:
        """True when this project's tasks are served from the database."""
        return self.for_project(project_id).on_sqlite

    def database_for(self, project_id: str) -> Path:
        """The file this project's rows live in. The whole precedence, in one place.

        The environment wins over everything, then the project's own entry, then the
        machine's fallback. Reading the variable here rather than trusting
        :attr:`database` is what makes the override still mean *every* project after
        task-400: a project that named its own file would otherwise quietly ignore it.
        """
        override = os.environ.get(DATABASE_ENV)
        if override:
            return Path(override).expanduser().resolve()
        entry = self.for_project(project_id)
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

    @property
    def any_on_sqlite(self) -> bool:
        """True when at least one project has been cut over.

        The server asks this to decide whether to open the database at all: a machine
        with nothing migrated should not create an empty store as a side effect of
        starting.
        """
        return any(entry.on_sqlite for entry in self.projects.values())

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
    for project_id, entry in raw_projects.items():
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise StorageConfigError(
                f"Invalid storage configuration at {path}: entry for {project_id!r} "
                "must be a mapping."
            )
        backend = entry.get("backend", FILES)
        if backend not in BACKENDS:
            raise StorageConfigError(
                f"Invalid storage configuration at {path}: unknown backend {backend!r} "
                f"for {project_id!r}. Known backends: {', '.join(BACKENDS)}."
            )
        configured_database = entry.get("database")
        if configured_database is not None and not isinstance(configured_database, str):
            raise StorageConfigError(
                f"Invalid storage configuration at {path}: 'database' for "
                f"{project_id!r} must be a string."
            )
        projects[str(project_id)] = ProjectStorage(
            backend=backend,
            cutover_at=entry.get("cutover_at"),
            source=entry.get("source"),
            database=configured_database,
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
        backend=SQLITE,
        cutover_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source=str(Path(source).resolve()),
        database=resolved_database,
    )
    updated = settings.with_project(project_id, entry)
    updated.save()
    return updated


def record_database(
    project_id: str, database: Path, *, home: Optional[Path] = None
) -> StorageSettings:
    """Point one project at a file of its own, leaving the rest of its entry alone.

    What ``storage split`` writes once the copy verifies, and the reason it is separate
    from :func:`record_cutover` is that a split is not a migration: the backend, the
    source directory and the cutover timestamp are all still true of the project and
    rewriting them would lose when it actually moved off files.
    """
    settings = load_storage_settings(home)
    entry = settings.for_project(project_id)
    updated = settings.with_project(
        project_id,
        replace(entry, database=str(Path(database).expanduser().resolve())),
    )
    updated.save()
    return updated


def record_rollback(project_id: str, *, home: Optional[Path] = None) -> StorageSettings:
    """Point one project back at its files, keeping the record of what it was on.

    The entry is retained rather than deleted, with ``backend: files``, so a rolled-back
    project still says which directory is authoritative and when it was last cut over.
    An entry that vanishes leaves an operator unable to tell "never migrated" from
    "migrated and rolled back", which are very different situations to be in.
    """
    settings = load_storage_settings(home)
    entry = settings.for_project(project_id)
    updated = settings.with_project(project_id, replace(entry, backend=FILES))
    updated.save()
    return updated


__all__ = [
    "BACKENDS",
    "DATABASES_DIRNAME",
    "DATABASE_ENV",
    "DEFAULT_DATABASE_NAME",
    "FILES",
    "SQLITE",
    "STORAGE_FILENAME",
    "ProjectStorage",
    "StorageConfigError",
    "StorageSettings",
    "default_database",
    "default_project_database",
    "load_storage_settings",
    "record_cutover",
    "record_database",
    "record_rollback",
]
