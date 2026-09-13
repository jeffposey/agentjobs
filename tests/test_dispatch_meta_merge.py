"""A run's meta is merged under a lock, and a terminal status stays terminal (task-264).

``write_yaml_atomically`` removed torn reads and said plainly what it did not remove: a
lost update between two read-merge-replace writers. That is the shape of the poll tick
that wrote ``status: stalled`` over a cancellation, so it is now closed.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from agentjobs.dispatch.atomic_yaml import merge_yaml_atomically
from agentjobs.dispatch.runner import RunDirectory, finish_stamped


def test_concurrent_writers_of_different_fields_lose_nothing(tmp_path: Path) -> None:
    directory = RunDirectory.create(tmp_path, "run_a", {"run_id": "run_a", "status": "running"})
    writers = 6
    writes = 40

    def write(index: int) -> None:
        for step in range(writes):
            directory.update_meta(**{f"writer_{index}_{step}": step})

    threads = [threading.Thread(target=write, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    meta = yaml.safe_load((directory.path / "meta.yaml").read_text(encoding="utf-8"))
    missing = [
        f"writer_{index}_{step}"
        for index in range(writers)
        for step in range(writes)
        if f"writer_{index}_{step}" not in meta
    ]
    assert missing == [], f"{len(missing)} updates were lost"
    assert not list(directory.path.glob("*.lock")), "the merge lock is released"


def test_a_terminal_status_outcome_and_finish_time_are_written_once(tmp_path: Path) -> None:
    path = tmp_path / "meta.yaml"
    merge_yaml_atomically(path, {"status": "running"}, merge=finish_stamped)
    ended = merge_yaml_atomically(
        path,
        {"status": "cancelled", "outcome": "cancelled", "finished_at": "2026-09-13T00:00:00+00:00"},
        merge=finish_stamped,
    )
    later = merge_yaml_atomically(
        path,
        {"status": "finished", "outcome": "interrupted", "finished_at": "later", "reaped": True},
        merge=finish_stamped,
    )
    assert later["status"] == ended["status"] == "cancelled"
    assert later["outcome"] == "cancelled"
    assert later["finished_at"] == "2026-09-13T00:00:00+00:00"
    assert later["reaped"] is True


def test_a_merge_lock_left_by_a_dead_writer_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    from agentjobs.dispatch import atomic_yaml

    path = tmp_path / "meta.yaml"
    (tmp_path / "meta.yaml.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(atomic_yaml, "MERGE_LOCK_STALE_SECONDS", -1.0)
    merged = merge_yaml_atomically(path, {"status": "running"}, merge=finish_stamped)
    assert merged == {"status": "running"}
