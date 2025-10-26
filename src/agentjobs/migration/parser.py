"""Markdown task file parser for migration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    objectives: List[str] = field(default_factory=list)
    deliverables: List[Dict[str, str]] = field(default_factory=list)
    phases: List[Dict[str, Optional[str]]] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    notes: str = ""
    human_summary: str = ""

    # Raw content for fallback
    raw_content: str = ""
    source_file: Optional[Path] = None


class MarkdownTaskParser:
    """Parse Markdown task files into structured data."""

    METADATA_PATTERNS = {
        "task_id": r"(?:ID|Task ID):\s*([^\n]+)",
        "status": r"(?:Status):\s*([^\n]+)",
        "priority": r"(?:Priority):\s*([^\n]+)",
        "category": r"(?:Category):\s*([^\n]+)",
        "estimated_effort": r"(?:Estimated (?:Duration|Effort)):\s*([^\n]+)",
        "assigned_to": r"(?:Assigned(?:\s+To)?|Owner):\s*([^\n]+)",
        "branch": r"(?:Branch):\s*([^\n]+)",
        "completion_date": r"(?:Completion Date|Date Completed):\s*([^\n]+)",
    }

    def parse_file(self, file_path: Path) -> ParsedTask:
        """Parse a markdown task file."""
        content = file_path.read_text(encoding="utf-8")

        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else file_path.stem

        metadata = self._extract_metadata(content)

        description = self._extract_section(
            content, ["Objective", "Description", "Goals", "Context"]
        )
        objectives = self._extract_list_items(content, ["Objectives", "Goals"])
        deliverables = self._extract_deliverables(content)
        phases = self._extract_phases(content)
        issues = self._extract_list_items(
            content, ["Issues", "Known Issues", "Blockers"]
        )
        notes = self._extract_section(
            content, ["Notes", "Additional Notes", "Comments", "Summary"]
        )
        human_summary = self._extract_human_summary(content)

        parsed = ParsedTask(
            title=title,
            source_file=file_path,
            raw_content=content,
            description=description,
            objectives=objectives,
            deliverables=deliverables,
            phases=phases,
            issues=issues,
            notes=notes,
            human_summary=human_summary,
            **metadata,
        )
        parsed.description = self._build_clean_description(parsed)
        return parsed

    def _extract_metadata(self, content: str) -> Dict[str, Optional[str]]:
        """Extract metadata fields from content."""
        metadata: Dict[str, Optional[str]] = {}
        metadata_block = re.sub(r"\*\*(.*?)\*\*", r"\1", content)
        metadata_block = re.sub(r"`([^`]+)`", r"\1", metadata_block)
        metadata_block = re.sub(r"__([^_]+)__", r"\1", metadata_block)
        for key, pattern in self.METADATA_PATTERNS.items():
            match = re.search(pattern, metadata_block, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                value = re.sub(r"[`*#_]", "", value)
                metadata[key] = value
        return metadata

    def _extract_section(self, content: str, section_headings: List[str]) -> str:
        """Extract content from a markdown section."""
        for heading in section_headings:
            pattern = rf"^##\s+{re.escape(heading)}[^\n]*\n(.*?)(?=^##|\Z)"
            match = re.search(
                pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE
            )
            if match:
                section_content = match.group(1).strip()
                return section_content
        return ""

    def _extract_human_summary(self, content: str) -> str:
        """Extract concise human-readable summary from task content."""
        summary_patterns = [
            r"##\s+Summary\s*\n([^\n#]+)",
            r"##\s+Overview\s*\n([^\n#]+)",
            r"##\s+Problem\s*\n([^\n#]+)",
        ]

        for pattern in summary_patterns:
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if match:
                summary = match.group(1).strip()
                text = self._trim_to_sentences(summary, max_sentences=2)
                if text:
                    return text

        desc = self._extract_section(content, ["Objective", "Description"])
        if desc:
            clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", desc)
            clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
            clean = re.sub(r"`([^`]+)`", r"\1", clean)
            text = self._trim_to_sentences(clean, max_chars=200)
            if text:
                return text

        return "No summary available"

    @staticmethod
    def _trim_to_sentences(
        text: str,
        *,
        max_sentences: int = 1,
        max_chars: Optional[int] = None,
    ) -> str:
        """Trim text to a limited number of sentences and characters."""
        normalized = " ".join(text.split())
        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        selected = " ".join(sentence.strip() for sentence in sentences[:max_sentences] if sentence.strip())
        if not selected:
            selected = normalized[: max_chars or len(normalized)]
        if max_chars is not None and len(selected) > max_chars:
            selected = selected[: max_chars - 3].rstrip() + "..."
        if selected and selected[-1] not in ".!?":
            selected += "."
        return selected

    def _build_clean_description(self, parsed: ParsedTask) -> str:
        """Build clean description from parsed content."""
        desc = parsed.description

        if desc and (len(desc) > 500 or "##" in desc or "Phase" in desc):
            lines = desc.split("\n")
            clean_lines: List[str] = []
            for line in lines:
                if re.match(r"^#+\s+Phase", line, re.IGNORECASE):
                    break
                if line.strip().startswith("**") and len(line) > 100:
                    continue
                clean_lines.append(line)
                if len("\n".join(clean_lines)) > 300:
                    break
            desc = "\n".join(clean_lines).strip()

        if not desc or len(desc) < 20:
            desc = (
                parsed.raw_content[:300] + "..."
                if len(parsed.raw_content) > 300
                else parsed.raw_content
            )

        return desc

    def _extract_list_items(
        self, content: str, section_headings: List[str]
    ) -> List[str]:
        """Extract list items from a section."""
        section_content = self._extract_section(content, section_headings)
        if not section_content:
            return []

        items = re.findall(
            r"^[-*]\s+(?:\[[ xX]\]\s+)?(.+)$", section_content, re.MULTILINE
        )
        return [self._clean_markdown(item) for item in items]

    def _extract_deliverables(self, content: str) -> List[Dict[str, str]]:
        """Extract deliverables with checkbox status."""
        section_content = self._extract_section(
            content, ["Deliverables", "Deliverables Completed", "Checklist"]
        )
        if not section_content:
            return []

        items = re.findall(
            r"^[-*]\s+(?:\[(?P<status>[ xX✓])\]\s+)?(?P<item>.+)$",
            section_content,
            re.MULTILINE,
        )
        deliverables: List[Dict[str, str]] = []
        for status, item in items:
            item_clean = self._clean_markdown(item)
            normalized = status.strip().lower() if status else ""
            if normalized in {"x", "✓"}:
                deliverable_status = "completed"
            else:
                deliverable_status = "pending"
            deliverables.append(
                {
                    "description": item_clean,
                    "status": deliverable_status,
                }
            )
        return deliverables

    def _extract_phases(self, content: str) -> List[Dict[str, Optional[str]]]:
        """Extract phase information from progress sections."""
        phase_pattern = re.compile(
            r"^###\s+(?P<prefix>[✅🔄⏸️❌✔️\-\s]*)?"
            r"Phase\s+(?P<identifier>[^\s:]+)"
            r"[:\s]+(?P<title>.+)$",
            re.MULTILINE | re.IGNORECASE,
        )

        matches = list(phase_pattern.finditer(content))
        phases: List[Dict[str, Optional[str]]] = []

        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(
                content
            )
            block = content[start:end].strip()
            heading_text = match.group(0)
            phase_id = match.group("identifier").strip()
            phase_title = match.group("title").strip()
            phase_title = re.sub(
                r"\((COMPLETE|IN PROGRESS|BLOCKED|NOT STARTED)\)",
                "",
                phase_title,
                flags=re.IGNORECASE,
            ).strip()
            status = self._detect_phase_status(heading_text)

            phases.append(
                {
                    "id": f"phase-{phase_id.lower()}",
                    "title": phase_title,
                    "status": status,
                    "notes": block or None,
                }
            )

        return phases

    @staticmethod
    def _clean_markdown(value: str) -> str:
        """Remove basic markdown formatting tokens from a string."""
        value = value.strip()
        value = re.sub(r"`([^`]+)`", r"\1", value)
        value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
        value = re.sub(r"\*(.+?)\*", r"\1", value)
        return value.strip()

    @staticmethod
    def _detect_phase_status(heading: str) -> str:
        """Infer phase status from heading content."""
        heading_lower = heading.lower()
        if "✅" in heading or "complete" in heading_lower or "done" in heading_lower:
            return "completed"
        if "🔄" in heading or "in progress" in heading_lower:
            return "in_progress"
        if "⏸" in heading or "blocked" in heading_lower or "paused" in heading_lower:
            return "blocked"
        if "❌" in heading_lower or "not started" in heading_lower:
            return "planned"
        return "planned"
