"""Cutting one rule out of a copy of the always-loaded bundle.

The bundle is the ``@`` chain a session in this repository loads: ``CLAUDE.md`` imports
``AGENTS.md``, ``ENGINEERING.md`` and ``ALLAGENTS.md``, and all four are read before the
first turn. An ablation arm is that same chain with one rule removed and nothing else
touched, so that any difference in what the agent does is attributable to the removal.

Two granularities, because one is not enough:

**A section**, addressed by its markdown heading text. This is the unit task-305 will move
text at, so a ``decorative`` verdict here names something that child can act on directly.
A section runs from its heading to the next heading of the same or shallower depth.

**A line**, addressed by a regular expression. Most rules in this bundle are stated in
more than one place -- audit 1's redundancy map counts 18 rules across 58 statements --
and ablating only the rule's home section leaves the restatement behind, which would make
a load-bearing rule look decorative. Line removal takes the strays without deleting the
sections that host them, several of which are the spine of the workflow.

**Neither is trusted.** :func:`ablate` returns the rendered arm, and the caller is
expected to run :func:`residual_hits` against it: a case declares the patterns that must
no longer appear anywhere in the arm, and a case whose ablation left one behind is a
misconfigured experiment rather than a result. That check is what turns "I think I removed
the rule" into something a reader can verify from the committed record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

BUNDLE_FILES: Tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "ENGINEERING.md",
    "ALLAGENTS.md",
)
"""The repository half of the chain, in load order.

``CLAUDE.md`` is the entry point Claude Code reads; the other three are what it imports.
The user half -- ``~/.claude/CLAUDE.md`` and the ``GLOBAL-AGENTS.md`` it imports -- is
deliberately absent: it is private, it loads in both arms regardless of what this package
does, and it therefore cannot be ablated. See :func:`global_agents_hits`.
"""

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")


@dataclass(frozen=True)
class Section:
    """One markdown section: its heading, its depth, and the lines it spans."""

    heading: str
    """The heading text with the ``#`` markers and surrounding whitespace stripped."""

    level: int
    """How many ``#`` markers the heading carried. 1 for a title, 2 for ``##``, and so on."""

    start: int
    """Index of the heading line in the file's line list."""

    end: int
    """Index one past the last line of the section, exclusive."""


@dataclass(frozen=True)
class SectionRef:
    """Remove the section under this heading, in this file."""

    file: str
    heading: str


@dataclass(frozen=True)
class LineRef:
    """Remove every line of this file that matches this pattern."""

    file: str
    pattern: str


@dataclass(frozen=True)
class AblationSpec:
    """What one arm removes. An empty spec is the ``with`` arm: the bundle unmodified."""

    sections: Tuple[SectionRef, ...] = ()
    lines: Tuple[LineRef, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sections and not self.lines


@dataclass
class AblationResult:
    """A rendered arm, and an account of what it took out.

    ``removed_lines`` is the arithmetic a reader can check against the diff: an ablation
    that reports zero removed lines did nothing, which is a bug in the case rather than a
    finding about the rule.
    """

    files: Dict[str, str] = field(default_factory=dict)
    removed_sections: List[str] = field(default_factory=list)
    removed_lines: int = 0
    unmatched: List[str] = field(default_factory=list)
    """Targets that matched nothing. A case naming a heading that no longer exists has
    gone stale against the file it is testing, and that is worth failing over rather than
    quietly running an arm identical to the control."""


def read_bundle(repo_root: Path) -> Dict[str, str]:
    """Read the repository half of the chain out of a checkout.

    Missing files are omitted rather than raising: the set is small and stable, but a
    caller pointed at the wrong directory should get an empty-looking bundle it can
    notice, not a traceback from three frames down.
    """
    bundle: Dict[str, str] = {}
    for name in BUNDLE_FILES:
        path = repo_root / name
        if path.is_file():
            bundle[name] = path.read_text(encoding="utf-8")
    return bundle


def split_sections(text: str) -> List[Section]:
    """Every markdown heading in ``text``, with the span of lines it owns.

    A section ends at the next heading of the same or shallower depth, so removing a
    ``##`` takes its ``###`` children with it -- which is what "cut this section" means to
    anyone reading the file.

    Fenced code blocks are respected. ``ENGINEERING.md`` is full of shell examples with
    ``# comment`` lines in them, and treating one of those as a heading would make a
    section boundary land in the middle of a command.
    """
    lines = text.splitlines()
    fenced = False
    found: List[Tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2).strip()))

    sections: List[Section] = []
    for position, (start, level, heading) in enumerate(found):
        end = len(lines)
        for later_start, later_level, _ in found[position + 1 :]:
            if later_level <= level:
                end = later_start
                break
        sections.append(Section(heading=heading, level=level, start=start, end=end))
    return sections


def _normalise(heading: str) -> str:
    """Compare headings ignoring backticks, case and inner whitespace.

    A case says ``Task files live on main, always``; the file says ``Task files live on
    `main`, always``. Requiring the author to reproduce the backticks exactly would make
    the suite fragile for no gain, and the normalised form is still specific enough that
    two different headings in these files never collide.
    """
    return re.sub(r"\s+", " ", heading.replace("`", "").replace("*", "")).strip().lower()


def ablate(bundle: Dict[str, str], spec: AblationSpec) -> AblationResult:
    """Apply one arm's removals to a bundle, reporting what actually came out."""
    result = AblationResult(files=dict(bundle))
    if spec.is_empty:
        return result

    by_file_sections: Dict[str, List[str]] = {}
    for ref in spec.sections:
        by_file_sections.setdefault(ref.file, []).append(ref.heading)
    by_file_lines: Dict[str, List[str]] = {}
    for line_ref in spec.lines:
        by_file_lines.setdefault(line_ref.file, []).append(line_ref.pattern)

    for name in set(by_file_sections) | set(by_file_lines):
        text = result.files.get(name)
        if text is None:
            result.unmatched.append(f"{name} (no such file in the bundle)")
            continue

        lines = text.splitlines()
        drop: set = set()

        for heading in by_file_sections.get(name, []):
            wanted = _normalise(heading)
            matches = [s for s in split_sections(text) if _normalise(s.heading) == wanted]
            if not matches:
                result.unmatched.append(f"{name}#{heading}")
                continue
            for section in matches:
                drop.update(range(section.start, section.end))
                result.removed_sections.append(f"{name}#{section.heading}")

        for pattern in by_file_lines.get(name, []):
            regex = re.compile(pattern, re.IGNORECASE)
            hit = False
            for index, line in enumerate(lines):
                if regex.search(line):
                    drop.add(index)
                    hit = True
            if not hit:
                result.unmatched.append(f"{name}:/{pattern}/")

        kept = [line for index, line in enumerate(lines) if index not in drop]
        result.removed_lines += len(lines) - len(kept)
        result.files[name] = "\n".join(kept) + ("\n" if text.endswith("\n") else "")

    return result


def residual_hits(files: Dict[str, str], patterns: Sequence[str]) -> Dict[str, List[str]]:
    """Which forbidden patterns survived the ablation, and in which files.

    An empty mapping is the only acceptable answer for an ablated arm. Anything else means
    the arm still states the rule somewhere, so the comparison would measure the removal of
    one *statement* of a rule rather than the removal of the rule -- which is precisely the
    error audit 1's redundancy map predicts, and precisely the error that would report a
    load-bearing rule as decorative.
    """
    hits: Dict[str, List[str]] = {}
    for pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        for name, text in sorted(files.items()):
            if regex.search(text):
                hits.setdefault(pattern, []).append(name)
    return hits


def global_agents_hits(patterns: Sequence[str], path: Path) -> Dict[str, bool]:
    """Whether the private global file states the rule too.

    ``~/.claude/CLAUDE.md`` and the ``GLOBAL-AGENTS.md`` it imports load in **both** arms
    and cannot be removed without breaking authentication, so they are a constant of the
    experiment rather than something this package controls. A constant is harmless while it
    is silent about the rule under test and fatal to the inference the moment it is not, so
    every run records the answer instead of assuming it. A ``True`` here is not a bug in the
    suite -- it is a reason to read that case's verdict as "decorative *given the global
    file*", and it is why the report prints it.
    """
    if not path.is_file():
        return {pattern: False for pattern in patterns}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {pattern: bool(re.search(pattern, text, re.IGNORECASE)) for pattern in patterns}
