"""Markdown-to-YAML migration entry point."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agentjobs.models_v2 import Priority
from agentjobs.queue import QUEUE_STEP, next_position
from agentjobs.storage import TaskStorage

from .converter import TaskConverter
from .parser import MarkdownTaskParser
from .reporter import MigrationResult

__all__ = ["migrate_tasks", "MigrationResult"]


def _collect_source_files(source_patterns: Sequence[str]) -> List[Path]:
    """Expand glob patterns into a de-duplicated list of files."""
    files: List[Path] = []
    seen: set[Path] = set()
    for pattern in source_patterns:
        for match in glob.glob(pattern, recursive=True):
            path = Path(match)
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def _claim_next_position(
    priority: Priority,
    cursor: Dict[Priority, int],
    storage: Optional[TaskStorage],
) -> int:
    """Take the next free position in ``priority``, remembering it for the next call.

    The first task to reach a band pays one corpus read to find its bottom; the rest
    step down from there. On a dry run there is no storage to read, so the band starts
    empty -- the numbers are then a preview, which is all a dry run promises.

    Read uncached, because the caller holds the queue lock and a snapshot taken before
    the lock was acquired is exactly the stale band the lock exists to rule out.
    """
    if priority not in cursor:
        existing = storage.list_tasks_uncached() if storage is not None else []
        cursor[priority] = next_position(existing, priority)
    else:
        cursor[priority] += QUEUE_STEP
    return cursor[priority]


def migrate_tasks(
    source_patterns: Sequence[str],
    target_dir: Path,
    prompts_dir: Path | None = None,
    dry_run: bool = False,
) -> List[MigrationResult]:
    """
    Migrate markdown task files to YAML format.

    Args:
        source_patterns: Glob patterns for source files.
        target_dir: Directory to write YAML files.
        prompts_dir: Directory containing prompt files to link.
        dry_run: If True, preview without writing files.

    Returns:
        List of migration results.
    """
    parser = MarkdownTaskParser()
    converter = TaskConverter()
    target_path = Path(target_dir)
    prompts_path = Path(prompts_dir) if prompts_dir is not None else None
    storage = TaskStorage(target_path) if not dry_run else None

    source_files = _collect_source_files(source_patterns)
    if storage is None:
        return _convert_all(source_files, parser, converter, target_path, prompts_path, None)
    # An import assigns positions, so it holds the queue lock for the whole run
    # (design section 7). Held across every file rather than per file: the band cursor
    # below is computed once and stepped, so a create landing halfway through would
    # take a number this run is about to hand to a later file.
    with storage.queue_lock():
        return _convert_all(source_files, parser, converter, target_path, prompts_path, storage)


def _convert_all(
    source_files: Sequence[Path],
    parser: "MarkdownTaskParser",
    converter: "TaskConverter",
    target_path: Path,
    prompts_path: Path | None,
    storage: Optional[TaskStorage],
) -> List[MigrationResult]:
    """Convert every source file. The caller holds the queue lock when writing."""
    dry_run = storage is None
    results: List[MigrationResult] = []
    # An import joins a queue that may already have tasks in it, and every task it
    # brings needs its own place in line. Seeded from the target directory the first
    # time a band is touched, then stepped locally: reading the corpus once per band
    # rather than once per file, and never handing two imported tasks one position.
    band_cursor: Dict[Priority, int] = {}

    for source_file in sorted(source_files):
        try:
            parsed = parser.parse_file(source_file)
            task = converter.convert(parsed, prompts_dir=prompts_path)
            if task.is_open:
                task.queue_position = _claim_next_position(task.priority, band_cursor, storage)

            warnings: List[str] = []
            description = task.spec.description or ""
            if not description.strip():
                warnings.append("Description is empty after migration")
            if len(description.strip()) < 10:
                warnings.append("Description is very short")
            if not task.deliverables:
                warnings.append("No deliverables extracted")

            target_file = target_path / f"{task.id}.yaml"
            if not dry_run and storage is not None:
                storage.save_task(task)

            results.append(
                MigrationResult(
                    source_file=source_file,
                    task_id=task.id,
                    success=True,
                    target_file=target_file,
                    warnings=warnings,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive branch
            results.append(
                MigrationResult(
                    source_file=source_file,
                    task_id=source_file.stem,
                    success=False,
                    errors=[str(exc)],
                )
            )

    return results
