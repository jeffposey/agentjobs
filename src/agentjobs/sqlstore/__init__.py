"""Authoritative SQLite storage for AgentJobs task records (task-273).

The design, its measurements and its rejected alternatives are in
``docs/storage-sqlite.md``. The boundary every backend satisfies is
:mod:`agentjobs.storage_protocol`.
"""

from .backup import restore, snapshot, verify
from .connection import Database, SqlStoreError, TaskLockTimeout
from .importer import (
    CorpusAlreadyImported,
    CorpusImporter,
    ImportReport,
    QuotationPolicyError,
)
from .migrations import MigrationReport, current_version, latest_version, upgrade
from .store import SqlTaskStore, TaskNotFound

__all__ = [
    "CorpusAlreadyImported",
    "CorpusImporter",
    "Database",
    "ImportReport",
    "MigrationReport",
    "QuotationPolicyError",
    "SqlStoreError",
    "TaskLockTimeout",
    "SqlTaskStore",
    "TaskNotFound",
    "current_version",
    "latest_version",
    "restore",
    "snapshot",
    "upgrade",
    "verify",
]
