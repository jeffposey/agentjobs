"""Migration report generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class MigrationResult:
    """Result of a single task migration."""

    source_file: Path
    task_id: str
    success: bool
    target_file: Optional[Path] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class MigrationReporter:
    """Generate migration reports."""

    def generate_report(
        self,
        results: List[MigrationResult],
        report_path: Path,
        dry_run: bool = False,
    ) -> None:
        """Generate markdown migration report."""
        successful = [result for result in results if result.success]
        failed = [result for result in results if not result.success]
        warnings = [result for result in results if result.warnings]

        report_lines: List[str] = []
        report_lines.append("# Migration Report\n")
        report_lines.append(
            f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        report_lines.append(
            f"**Mode**: {'Dry Run (Preview)' if dry_run else 'Live Migration'}\n"
        )
        report_lines.append("\n---\n")

        report_lines.append("## Summary\n")
        report_lines.append(f"- **Total Tasks**: {len(results)}\n")
        report_lines.append(f"- **Successful**: {len(successful)}\n")
        report_lines.append(f"- **Failed**: {len(failed)}\n")
        report_lines.append(f"- **With Warnings**: {len(warnings)}\n")
        report_lines.append("\n---\n")

        if successful:
            report_lines.append("## Successful Migrations\n")
            for result in successful:
                report_lines.append(f"### ✅ {result.task_id}\n")
                report_lines.append(f"- **Source**: `{result.source_file}`\n")
                if result.target_file:
                    report_lines.append(f"- **Target**: `{result.target_file}`\n")
                if result.warnings:
                    report_lines.append(f"- **Warnings**: {len(result.warnings)}\n")
                    for warning in result.warnings:
                        report_lines.append(f"  - ⚠️ {warning}\n")
                report_lines.append("\n")

        if failed:
            report_lines.append("## Failed Migrations\n")
            for result in failed:
                report_lines.append(f"### ❌ {result.task_id}\n")
                report_lines.append(f"- **Source**: `{result.source_file}`\n")
                if result.errors:
                    report_lines.append(f"- **Errors**:\n")
                    for error in result.errors:
                        report_lines.append(f"  - {error}\n")
                report_lines.append("\n")

        report_lines.append("## Recommendations\n")
        if dry_run:
            report_lines.append("- Review warnings and errors above.\n")
            report_lines.append("- Fix any critical issues in source files.\n")
            report_lines.append(
                "- Run migration without `--dry-run` to write YAML files.\n"
            )
        else:
            report_lines.append(
                "- Review generated YAML files in target directory.\n"
            )
            report_lines.append("- Verify task data integrity.\n")
            report_lines.append("- Test with `agentjobs serve` to view in browser.\n")

        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("".join(report_lines), encoding="utf-8")

