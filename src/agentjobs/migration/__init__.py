"""Markdown-to-YAML migration entry point."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import List, Sequence

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

    results: List[MigrationResult] = []
    source_files = _collect_source_files(source_patterns)

    for source_file in sorted(source_files):
        try:
            parsed = parser.parse_file(source_file)
            task = converter.convert(parsed, prompts_dir=prompts_path)

            warnings: List[str] = []
            if not task.description or not task.description.strip():
                warnings.append("Description is empty after migration")
            if len(task.description.strip()) < 10:
                warnings.append("Description is very short")
            if not task.phases and not task.deliverables:
                warnings.append("No phases or deliverables extracted")

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

