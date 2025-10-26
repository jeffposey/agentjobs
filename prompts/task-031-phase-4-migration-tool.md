# Task 031 - Phase 4: Migration Tool

---
## ⚠️ CRITICAL: WORKING DIRECTORY ⚠️

**YOU MUST WORK IN THE AGENTJOBS REPOSITORY:**
```bash
cd /mnt/c/projects/agentjobs
```

**DO NOT:**
- ❌ Work in `/mnt/c/projects/privateproject`
- ❌ Copy agentjobs files into privateproject
- ❌ Create `privateproject/src/agentjobs/` directory
- ❌ Make any changes to privateproject repo (yet - that's Phase 5)

**This prompt is stored in privateproject but the WORK is in agentjobs.**

---

**Context**: Phases 1-3 complete with core infrastructure, REST API, Python client, and browser GUI.
Now implement Phase 4: Migration tool to convert existing Markdown task files to structured YAML.

---

## Objectives

Build a migration tool that:
- Parses existing Markdown task files (PrivateProject format)
- Extracts metadata, sections, and structured content
- Converts to YAML format compatible with AgentJobs schema
- Links related prompt files from prompts directory
- Generates detailed migration report
- Supports dry-run mode for safe preview

**Target Use Case**: Migrate 30+ PrivateProject tasks from `ops/tasks/*.md` to structured YAML.

---

## Implementation Steps

**Before you start:**
```bash
cd /mnt/c/projects/agentjobs
git status  # Verify you're in agentjobs, NOT privateproject
```

---

## 1. CLI Command Structure

Add `migrate` command to `src/agentjobs/cli.py`:

```python
@app.command()
def migrate(
    source: List[str] = typer.Option(
        ...,
        "--from",
        "-f",
        help="Glob pattern(s) for source markdown files (e.g., 'ops/tasks/*.md')",
    ),
    target_dir: Path = typer.Option(
        ...,
        "--to",
        "-t",
        help="Target directory for YAML files",
    ),
    prompts_dir: Optional[Path] = typer.Option(
        None,
        "--prompts-dir",
        "-p",
        help="Directory containing related prompt files to link",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview migration without writing files",
    ),
    report_file: Path = typer.Option(
        Path("migration-report.md"),
        "--report",
        "-r",
        help="Path to save migration report",
    ),
) -> None:
    """Migrate Markdown task files to structured YAML format."""
    # Implementation here
```

---

## 2. Markdown Parser (`src/agentjobs/migration/parser.py`)

Create parser to extract task components from Markdown:

```python
"""Markdown task file parser for migration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ParsedTask:
    """Intermediate representation of parsed markdown task."""

    # Metadata from header
    title: str
    task_id: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    estimated_effort: Optional[str] = None
    assigned_to: Optional[str] = None
    branch: Optional[str] = None
    completion_date: Optional[str] = None

    # Content sections
    description: str = ""
    objectives: List[str] = None
    deliverables: List[str] = None
    phases: List[Dict] = None
    issues: List[str] = None
    notes: str = ""

    # Raw content for fallback
    raw_content: str = ""
    source_file: Optional[Path] = None

    def __post_init__(self):
        if self.objectives is None:
            self.objectives = []
        if self.deliverables is None:
            self.deliverables = []
        if self.phases is None:
            self.phases = []
        if self.issues is None:
            self.issues = []


class MarkdownTaskParser:
    """Parse Markdown task files into structured data."""

    # Regex patterns for metadata extraction
    METADATA_PATTERNS = {
        "task_id": r"(?:ID|Task ID):\s*([^\n]+)",
        "status": r"(?:Status):\s*([^\n]+)",
        "priority": r"(?:Priority):\s*([^\n]+)",
        "category": r"(?:Category):\s*([^\n]+)",
        "estimated_effort": r"(?:Estimated (?:Duration|Effort)):\s*([^\n]+)",
        "assigned_to": r"(?:Assigned|Owner):\s*([^\n]+)",
        "branch": r"(?:Branch):\s*([^\n]+)",
        "completion_date": r"(?:Completion Date|Date Completed):\s*([^\n]+)",
    }

    def parse_file(self, file_path: Path) -> ParsedTask:
        """Parse a markdown task file."""
        content = file_path.read_text(encoding="utf-8")

        # Extract title (first # heading)
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else file_path.stem

        # Extract metadata from header section
        metadata = self._extract_metadata(content)

        # Extract sections
        description = self._extract_section(content, ["Objective", "Description", "Goals", "Context"])
        objectives = self._extract_list_items(content, ["Objectives", "Goals"])
        deliverables = self._extract_deliverables(content)
        phases = self._extract_phases(content)
        issues = self._extract_list_items(content, ["Issues", "Known Issues", "Blockers"])

        return ParsedTask(
            title=title,
            source_file=file_path,
            raw_content=content,
            description=description,
            objectives=objectives,
            deliverables=deliverables,
            phases=phases,
            issues=issues,
            **metadata,
        )

    def _extract_metadata(self, content: str) -> Dict[str, Optional[str]]:
        """Extract metadata fields from content."""
        metadata = {}
        for key, pattern in self.METADATA_PATTERNS.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                # Clean up markdown formatting
                value = re.sub(r"\*\*|\*|`", "", value)
                metadata[key] = value
        return metadata

    def _extract_section(self, content: str, section_headings: List[str]) -> str:
        """Extract content from a markdown section."""
        for heading in section_headings:
            pattern = rf"^##\s+{heading}[^\n]*\n(.*?)(?=^##|\Z)"
            match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
            if match:
                section_content = match.group(1).strip()
                # Remove markdown formatting for description
                return section_content
        return ""

    def _extract_list_items(self, content: str, section_headings: List[str]) -> List[str]:
        """Extract list items from a section."""
        section_content = self._extract_section(content, section_headings)
        if not section_content:
            return []

        # Find list items (-, *, or checkboxes)
        items = re.findall(r"^[-*]\s+(?:\[[ x]\]\s+)?(.+)$", section_content, re.MULTILINE)
        return [item.strip() for item in items]

    def _extract_deliverables(self, content: str) -> List[str]:
        """Extract deliverables with checkbox status."""
        section_content = self._extract_section(content, ["Deliverables", "Deliverables Completed"])
        if not section_content:
            return []

        # Extract checkbox items with status
        items = re.findall(r"^[-*]\s+\[([ xX✓])\]\s+(.+)$", section_content, re.MULTILINE)
        deliverables = []
        for status, item in items:
            # Clean up markdown
            item = re.sub(r"`([^`]+)`", r"\1", item)
            deliverables.append({
                "description": item.strip(),
                "status": "completed" if status.strip().lower() in ["x", "✓"] else "pending",
            })
        return deliverables

    def _extract_phases(self, content: str) -> List[Dict]:
        """Extract phase information from progress sections."""
        phases = []

        # Look for phase headings (### Phase 1, ### ✅ Phase 1A, etc.)
        phase_pattern = r"^###\s+(?:✅|🔄|⏸️)?\s*Phase\s+(\w+)[:\s]+(.+?)$"
        matches = re.finditer(phase_pattern, content, re.MULTILINE | re.IGNORECASE)

        for match in matches:
            phase_id = match.group(1).strip()
            phase_title = match.group(2).strip()

            # Determine status from emoji or following content
            status = "planned"
            if "✅" in match.group(0) or "(COMPLETE)" in phase_title.upper():
                status = "completed"
            elif "🔄" in match.group(0) or "(IN PROGRESS)" in phase_title.upper():
                status = "in_progress"
            elif "⏸️" in match.group(0) or "(BLOCKED)" in phase_title.upper():
                status = "blocked"

            # Clean title
            phase_title = re.sub(r"\((?:COMPLETE|IN PROGRESS|BLOCKED|NOT STARTED)\)", "", phase_title, flags=re.IGNORECASE).strip()

            phases.append({
                "id": f"phase-{phase_id.lower()}",
                "title": phase_title,
                "status": status,
            })

        return phases
```

---

## 3. YAML Converter (`src/agentjobs/migration/converter.py`)

Convert parsed tasks to AgentJobs YAML format:

```python
"""Convert parsed markdown tasks to AgentJobs YAML format."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agentjobs.models import (
    Deliverable,
    Phase,
    Priority,
    Prompts,
    Task,
    TaskStatus,
)
from agentjobs.storage import TaskStorage

from .parser import ParsedTask


class TaskConverter:
    """Convert ParsedTask to AgentJobs Task model."""

    # Status mapping from common task statuses to TaskStatus enum
    STATUS_MAP = {
        "complete": TaskStatus.COMPLETED,
        "completed": TaskStatus.COMPLETED,
        "done": TaskStatus.COMPLETED,
        "in progress": TaskStatus.IN_PROGRESS,
        "in-progress": TaskStatus.IN_PROGRESS,
        "active": TaskStatus.IN_PROGRESS,
        "blocked": TaskStatus.BLOCKED,
        "on hold": TaskStatus.BLOCKED,
        "planned": TaskStatus.PLANNED,
        "pending": TaskStatus.PLANNED,
        "under review": TaskStatus.UNDER_REVIEW,
        "review": TaskStatus.UNDER_REVIEW,
    }

    # Priority mapping
    PRIORITY_MAP = {
        "critical": Priority.CRITICAL,
        "high": Priority.HIGH,
        "medium": Priority.MEDIUM,
        "low": Priority.LOW,
    }

    def convert(
        self,
        parsed: ParsedTask,
        prompts_dir: Optional[Path] = None,
    ) -> Task:
        """Convert ParsedTask to Task model."""

        # Generate task ID from filename or parsed ID
        task_id = self._generate_task_id(parsed)

        # Map status and priority
        status = self._map_status(parsed.status)
        priority = self._map_priority(parsed.priority)

        # Convert phases
        phases = [
            Phase(
                id=p["id"],
                title=p["title"],
                status=self._map_status(p.get("status")),
                notes=p.get("notes"),
            )
            for p in parsed.phases
        ]

        # Convert deliverables
        deliverables = [
            Deliverable(
                path=d.get("path", d["description"]),
                description=d.get("description", ""),
                status=d.get("status", "pending"),
            )
            for d in parsed.deliverables
        ]

        # Link prompt files
        prompts = self._find_prompts(parsed, prompts_dir)

        # Build task
        task = Task(
            id=task_id,
            title=parsed.title,
            description=parsed.description or parsed.raw_content[:500],
            status=status,
            priority=priority,
            category=parsed.category or "general",
            assigned_to=parsed.assigned_to,
            estimated_effort=parsed.estimated_effort,
            phases=phases if phases else None,
            deliverables=deliverables if deliverables else None,
            prompts=prompts,
            branches=[{"name": parsed.branch, "status": "active"}] if parsed.branch else None,
        )

        return task

    def _generate_task_id(self, parsed: ParsedTask) -> str:
        """Generate task ID from parsed data or filename."""
        if parsed.task_id:
            # Clean up existing ID
            task_id = parsed.task_id.lower().strip()
            task_id = task_id.replace("task-", "").replace("task", "").strip()
            if task_id.startswith("#"):
                task_id = task_id[1:]
            return f"task-{task_id}"

        # Extract from filename (e.g., task-016-feature-name.md)
        if parsed.source_file:
            stem = parsed.source_file.stem
            if stem.startswith("task-"):
                return stem

        # Fallback: slugify title
        slug = parsed.title.lower().replace(" ", "-")[:50]
        return f"task-{slug}"

    def _map_status(self, status_str: Optional[str]) -> TaskStatus:
        """Map status string to TaskStatus enum."""
        if not status_str:
            return TaskStatus.PLANNED

        status_lower = status_str.strip().lower()
        return self.STATUS_MAP.get(status_lower, TaskStatus.PLANNED)

    def _map_priority(self, priority_str: Optional[str]) -> Priority:
        """Map priority string to Priority enum."""
        if not priority_str:
            return Priority.MEDIUM

        priority_lower = priority_str.strip().lower()
        return self.PRIORITY_MAP.get(priority_lower, Priority.MEDIUM)

    def _find_prompts(
        self,
        parsed: ParsedTask,
        prompts_dir: Optional[Path],
    ) -> Optional[Prompts]:
        """Find and link related prompt files."""
        if not prompts_dir or not parsed.task_id:
            return None

        task_num = self._extract_task_number(parsed.task_id)
        if not task_num:
            return None

        # Find prompt files matching task-XXX-*.md pattern
        pattern = f"task-{task_num}-*.md"
        prompt_files = list(prompts_dir.glob(pattern))

        if not prompt_files:
            return None

        # Use first prompt as starter, rest as followups
        starter_file = prompt_files[0]
        starter_content = starter_file.read_text(encoding="utf-8")

        return Prompts(
            starter=starter_content,
            followups=[],  # Can be extended to parse multiple prompts
        )

    def _extract_task_number(self, task_id: str) -> Optional[str]:
        """Extract numeric part from task ID."""
        import re
        match = re.search(r"(\d+)", task_id)
        return match.group(1) if match else None
```

---

## 4. Migration Report Generator (`src/agentjobs/migration/reporter.py`)

Generate detailed migration report:

```python
"""Migration report generation."""

from __future__ import annotations

from dataclasses import dataclass
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
    errors: List[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.warnings is None:
            self.warnings = []


class MigrationReporter:
    """Generate migration reports."""

    def generate_report(
        self,
        results: List[MigrationResult],
        report_path: Path,
        dry_run: bool = False,
    ) -> None:
        """Generate markdown migration report."""

        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        warnings = [r for r in results if r.warnings]

        report = []
        report.append("# Migration Report\n")
        report.append(f"**Generated**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
        report.append(f"**Mode**: {'Dry Run (Preview)' if dry_run else 'Live Migration'}\n")
        report.append("\n---\n")

        # Summary
        report.append("## Summary\n")
        report.append(f"- **Total Tasks**: {len(results)}\n")
        report.append(f"- **Successful**: {len(successful)}\n")
        report.append(f"- **Failed**: {len(failed)}\n")
        report.append(f"- **With Warnings**: {len(warnings)}\n")
        report.append("\n---\n")

        # Successful migrations
        if successful:
            report.append("## Successful Migrations\n")
            for result in successful:
                report.append(f"### ✅ {result.task_id}\n")
                report.append(f"- **Source**: `{result.source_file}`\n")
                if result.target_file:
                    report.append(f"- **Target**: `{result.target_file}`\n")
                if result.warnings:
                    report.append(f"- **Warnings**: {len(result.warnings)}\n")
                    for warning in result.warnings:
                        report.append(f"  - ⚠️ {warning}\n")
                report.append("\n")

        # Failed migrations
        if failed:
            report.append("## Failed Migrations\n")
            for result in failed:
                report.append(f"### ❌ {result.task_id}\n")
                report.append(f"- **Source**: `{result.source_file}`\n")
                report.append(f"- **Errors**:\n")
                for error in result.errors:
                    report.append(f"  - {error}\n")
                report.append("\n")

        # Recommendations
        report.append("## Recommendations\n")
        if dry_run:
            report.append("- Review warnings and errors above\n")
            report.append("- Fix any critical issues in source files\n")
            report.append("- Run migration without `--dry-run` to write YAML files\n")
        else:
            report.append("- Review generated YAML files in target directory\n")
            report.append("- Verify task data integrity\n")
            report.append("- Test with `agentjobs serve` to view in browser\n")

        # Write report
        report_path.write_text("".join(report), encoding="utf-8")
```

---

## 5. Main Migration Logic (`src/agentjobs/migration/__init__.py`)

Orchestrate the migration process:

```python
"""Task migration from Markdown to YAML."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import List

from agentjobs.storage import TaskStorage

from .converter import TaskConverter
from .parser import MarkdownTaskParser, ParsedTask
from .reporter import MigrationReporter, MigrationResult


def migrate_tasks(
    source_patterns: List[str],
    target_dir: Path,
    prompts_dir: Path | None = None,
    dry_run: bool = False,
) -> List[MigrationResult]:
    """
    Migrate markdown task files to YAML format.

    Args:
        source_patterns: Glob patterns for source files
        target_dir: Directory to write YAML files
        prompts_dir: Directory containing prompt files to link
        dry_run: If True, preview without writing files

    Returns:
        List of migration results
    """
    parser = MarkdownTaskParser()
    converter = TaskConverter()
    storage = TaskStorage(tasks_dir=target_dir)

    results = []

    # Collect all source files
    source_files = []
    for pattern in source_patterns:
        source_files.extend(glob.glob(pattern, recursive=True))

    for source_path in source_files:
        source_file = Path(source_path)

        try:
            # Parse markdown
            parsed = parser.parse_file(source_file)

            # Convert to Task model
            task = converter.convert(parsed, prompts_dir=prompts_dir)

            # Validate
            warnings = []
            if not task.description or len(task.description) < 10:
                warnings.append("Description is very short or empty")
            if not task.phases and not task.deliverables:
                warnings.append("No phases or deliverables found")

            # Save (if not dry run)
            target_file = None
            if not dry_run:
                storage.save_task(task)
                target_file = target_dir / f"{task.id}.yaml"

            results.append(
                MigrationResult(
                    source_file=source_file,
                    task_id=task.id,
                    success=True,
                    target_file=target_file,
                    warnings=warnings,
                )
            )

        except Exception as exc:
            results.append(
                MigrationResult(
                    source_file=source_file,
                    task_id=source_file.stem,
                    success=False,
                    errors=[str(exc)],
                )
            )

    return results
```

---

## 6. Wire Into CLI

Update `src/agentjobs/cli.py` to add migrate command:

```python
from agentjobs.migration import migrate_tasks
from agentjobs.migration.reporter import MigrationReporter


@app.command()
def migrate(
    source: List[str] = typer.Option(
        ...,
        "--from",
        "-f",
        help="Glob pattern(s) for source markdown files",
    ),
    target_dir: Path = typer.Option(
        ...,
        "--to",
        "-t",
        help="Target directory for YAML files",
    ),
    prompts_dir: Optional[Path] = typer.Option(
        None,
        "--prompts-dir",
        "-p",
        help="Directory containing prompt files",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview migration without writing",
    ),
    report_file: Path = typer.Option(
        Path("migration-report.md"),
        "--report",
        "-r",
        help="Path for migration report",
    ),
) -> None:
    """Migrate Markdown task files to YAML."""

    # Ensure target directory exists
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    # Run migration
    typer.echo(f"{'[DRY RUN] ' if dry_run else ''}Migrating tasks...")
    results = migrate_tasks(source, target_dir, prompts_dir, dry_run)

    # Generate report
    reporter = MigrationReporter()
    reporter.generate_report(results, report_file, dry_run)

    # Print summary
    successful = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)

    typer.echo(f"\n✓ Migration complete!")
    typer.echo(f"  Successful: {successful}")
    typer.echo(f"  Failed: {failed}")
    typer.echo(f"  Report: {report_file}")

    if dry_run:
        typer.echo("\n⚠️  This was a dry run - no files were written.")
```

---

## 7. Testing

Create tests in `tests/test_migration.py`:

```python
"""Tests for migration functionality."""

from pathlib import Path
from textwrap import dedent

import pytest

from agentjobs.migration.parser import MarkdownTaskParser
from agentjobs.migration.converter import TaskConverter


@pytest.fixture
def sample_markdown(tmp_path):
    """Create sample markdown task file."""
    content = dedent("""
    # Task 016: Feature Implementation

    **Status**: Complete
    **Priority**: High
    **Category**: Feature Development
    **Estimated Effort**: 2-3 weeks
    **Assigned**: Codex
    **Branch**: feature/task-016-feature-name

    ## Objective

    Build a new feature that does XYZ.

    ## Deliverables

    - [x] Core implementation
    - [ ] Tests
    - [x] Documentation

    ### ✅ Phase 1: Planning (COMPLETE)
    Design and specification work.

    ### 🔄 Phase 2: Implementation (IN PROGRESS)
    Core development work.
    """)

    task_file = tmp_path / "task-016-feature.md"
    task_file.write_text(content)
    return task_file


def test_parse_markdown_task(sample_markdown):
    """Test parsing markdown task file."""
    parser = MarkdownTaskParser()
    parsed = parser.parse_file(sample_markdown)

    assert parsed.title == "Task 016: Feature Implementation"
    assert parsed.status == "Complete"
    assert parsed.priority == "High"
    assert parsed.category == "Feature Development"
    assert len(parsed.phases) == 2
    assert len(parsed.deliverables) == 3


def test_convert_to_yaml_task(sample_markdown):
    """Test converting parsed task to YAML."""
    parser = MarkdownTaskParser()
    converter = TaskConverter()

    parsed = parser.parse_file(sample_markdown)
    task = converter.convert(parsed)

    assert task.id.startswith("task-")
    assert task.status.value == "completed"
    assert task.priority.value == "high"
    assert len(task.phases) == 2
    assert task.phases[0].status.value == "completed"
    assert task.phases[1].status.value == "in_progress"
```

---

## Acceptance Criteria

- [ ] `agentjobs migrate` command functional
- [ ] Parses Markdown task files correctly
- [ ] Extracts metadata (status, priority, category, etc.)
- [ ] Converts phases with status detection
- [ ] Converts deliverables with checkbox status
- [ ] Links prompt files from prompts directory
- [ ] Generates detailed migration report
- [ ] `--dry-run` mode works (no files written)
- [ ] Handles edge cases (missing metadata, malformed markdown)
- [ ] Tests pass with >80% coverage

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Create sample test data (optional - use PrivateProject files later)
mkdir -p tests/fixtures/markdown_tasks
echo "# Sample Task
**Status**: Complete
**Priority**: High

## Objective
Test migration." > tests/fixtures/markdown_tasks/task-001-sample.md

# Dry run test
.venv/bin/agentjobs migrate \
  --from "tests/fixtures/markdown_tasks/*.md" \
  --to "/tmp/migrated_tasks" \
  --dry-run \
  --report migration-report.md

# Review report
cat migration-report.md

# Actual migration test
.venv/bin/agentjobs migrate \
  --from "tests/fixtures/markdown_tasks/*.md" \
  --to "/tmp/migrated_tasks" \
  --report migration-report.md

# Verify YAML output
ls -la /tmp/migrated_tasks/
cat /tmp/migrated_tasks/task-001-sample.yaml

# Run tests
.venv/bin/pytest tests/test_migration.py -v
```

---

## Commit When Done

**VERIFY YOU ARE IN THE CORRECT REPOSITORY:**
```bash
pwd  # Should show: /mnt/c/projects/agentjobs
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(migration): implement Phase 4 migration tool

Add markdown-to-YAML migration with dry-run and reporting.

Features:
- Parse Markdown task files (PrivateProject format)
- Extract metadata, phases, deliverables
- Convert to AgentJobs YAML schema
- Link prompt files from prompts directory
- Generate detailed migration report
- Dry-run mode for safe preview

Components:
- migration/parser.py (Markdown parsing)
- migration/converter.py (YAML conversion)
- migration/reporter.py (Report generation)
- CLI migrate command with options
- Tests for parser and converter

Usage:
  agentjobs migrate \\
    --from 'ops/tasks/*.md' \\
    --to tasks/ \\
    --prompts-dir ops/prompts \\
    --dry-run

Claude with Sonnet 4.5"

git push origin main
```

---

## Notes

- **Parser is flexible**: Handles various Markdown formats (different heading levels, metadata positions)
- **Status detection**: Maps common status strings to TaskStatus enum
- **Phase extraction**: Detects emoji prefixes (✅, 🔄, ⏸️) and status keywords
- **Deliverables**: Preserves checkbox status (completed vs pending)
- **Prompt linking**: Automatically finds related prompt files by task number
- **Error handling**: Captures parse failures in migration report
- **Dry-run**: Essential for previewing migration before committing
- **Report**: Detailed summary with warnings, errors, recommendations

**Phase 5 will use this tool** to migrate PrivateProject's 30+ tasks.

Ready to build! 🔧
