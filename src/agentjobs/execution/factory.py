"""One place a machine home becomes an execution journal.

The counterpart to ``store_factory`` for task records, and deliberately separate from it:
task databases are opened by the server alone (task-273), while this journal is opened
by every process that dispatches, because admission across those processes is the thing
it exists to make atomic. Call sites never compose the path; they ask here.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Tuple

from agentjobs.execution.store import EXECUTION_DB_FILENAME, ExecutionStore

_stores: Dict[Tuple[str, int], ExecutionStore] = {}
_lock = threading.Lock()


def execution_db_path(home: Path) -> Path:
    """Where a machine home keeps its journal: beside the registry and the runs root."""
    return Path(home) / EXECUTION_DB_FILENAME


def execution_store_for(home: Path) -> ExecutionStore:
    """The process's journal for one machine home, opened on first use.

    Cached per path *and pid*: a forked child must open its own connection, since a
    SQLite handle carried across ``fork`` is undefined behaviour.
    """
    path = execution_db_path(home).expanduser().resolve()
    key = (os.path.normcase(str(path)), os.getpid())
    with _lock:
        store = _stores.get(key)
        if store is None:
            store = ExecutionStore(path)
            _stores[key] = store
        return store


def close_execution_stores() -> None:
    """Close every cached journal. For tests and for shutdown, which own the process."""
    with _lock:
        for store in _stores.values():
            store.close()
        _stores.clear()


__all__ = ["close_execution_stores", "execution_db_path", "execution_store_for"]
