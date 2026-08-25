"""Loading and validating a scenario.

A case is a directory under ``evals/context/`` holding one ``case.yaml``. The file is the
whole specification: the incident that motivates the scenario, the sections the ablation
arm removes, the fixture repository to build, the prompt, and the checks that score the
result. Nothing about a case lives in a prompt string inside this package -- that is what
"the rubric is in the repository, not in a prompt" means, and it is what makes a baseline
run reproducible six months and one model later.

Validation is strict and refuses rather than defaults. A case with an unknown key, a
check of an unknown kind, or no ``incident`` is a case somebody edited without reading the
format, and running it would produce a number nobody should trust.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from agentjobs.contexteval.bundle import AblationSpec, LineRef, SectionRef
from agentjobs.contexteval.checks import CHECK_KINDS

SCHEMA_VERSION = "1"

_TOP_LEVEL = {
    "schema_version",
    "name",
    "rule",
    "home",
    "incident",
    "tags",
    "runs",
    "max_turns",
    "timeout_seconds",
    "ablate",
    "residual_forbidden",
    "fixture",
    "prompt",
    "compliant_when",
    "violated_when",
    "notes",
}
_INCIDENT_KEYS = {"what", "where"}
_ABLATE_KEYS = {"remove_sections", "remove_lines"}
_FIXTURE_KEYS = {"files", "uncommitted", "git", "worktrees"}

DEFAULT_RUNS = 3
DEFAULT_MAX_TURNS = 16
DEFAULT_TIMEOUT_SECONDS = 600


class CaseError(ValueError):
    """A case file that cannot be trusted to produce a meaningful number."""


@dataclass(frozen=True)
class Incident:
    """Why this scenario exists. Both halves are required by ac-2 of task-304."""

    what: str
    """What went wrong, in one sentence."""

    where: str
    """Where it is written down -- a task id, or the section of a bundle file that tells
    the story. A scenario nobody can trace to a real failure is a scenario somebody
    invented, and the spec forbids those."""


@dataclass(frozen=True)
class Check:
    """One mechanical predicate over a finished run."""

    kind: str
    args: Mapping[str, Any]
    negate: bool = False

    def describe(self) -> str:
        body = ", ".join(f"{k}={v!r}" for k, v in sorted(self.args.items()))
        return f"{'not ' if self.negate else ''}{self.kind}({body})"


@dataclass(frozen=True)
class Fixture:
    """The scratch repository a scenario is run against.

    Everything here is throwaway. No file in a fixture is copied from the live backlog and
    no task id in one refers to a real record -- task-304's constraint says so outright,
    and the reason is that a scenario about committing task records must be free to commit
    them wrongly.
    """

    files: Mapping[str, str] = field(default_factory=dict)
    """Paths written and committed on ``main`` before the session starts."""

    uncommitted: Mapping[str, str] = field(default_factory=dict)
    """Paths written and deliberately *not* committed -- a peer's in-flight work."""

    git: Sequence[Sequence[str]] = field(default_factory=tuple)
    """Extra git argument lists run in the clone after the initial commit."""

    worktrees: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    """Worktrees to create up front, for scenarios that begin with the agent already in one.

    Each entry is ``{name, branch}`` and may carry ``files`` and ``message``, which are
    written into the worktree and committed on its branch before the session starts. That
    matters more than it sounds: several of these scenarios are about what an agent does
    *after* the code is written, and making it write the code first spends the turn budget
    on setup and then scores a run that ran out of turns as if it had chosen restraint.
    """


@dataclass(frozen=True)
class Case:
    """A scenario, fully specified."""

    name: str
    path: Path
    rule: str
    home: str
    incident: Incident
    prompt: str
    ablation: AblationSpec
    residual_forbidden: Tuple[str, ...]
    fixture: Fixture
    compliant_when: Tuple[Check, ...]
    violated_when: Tuple[Check, ...]
    tags: Tuple[str, ...] = ()
    runs: int = DEFAULT_RUNS
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    notes: str = ""

    @property
    def cwd(self) -> str:
        """Where the session starts, relative to the sandbox root.

        A scenario that pre-creates a worktree starts the agent inside it; everything else
        starts in the clone.
        """
        if self.fixture.worktrees:
            return f"worktrees/{self.fixture.worktrees[0]['name']}"
        return "clone"


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise CaseError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _reject_unknown(mapping: Mapping[str, Any], allowed: set, where: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise CaseError(f"{where}: unknown field(s) {extra}; allowed: {sorted(allowed)}")


def _parse_checks(raw: Any, where: str) -> Tuple[Check, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CaseError(f"{where}: expected a list of checks")
    checks: List[Check] = []
    for index, item in enumerate(raw):
        position = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise CaseError(f"{position}: expected a mapping")
        args = dict(item)
        kind = args.pop("kind", None)
        negate = bool(args.pop("negate", False))
        if not isinstance(kind, str):
            raise CaseError(f"{position}: missing required field 'kind'")
        if kind not in CHECK_KINDS:
            raise CaseError(
                f"{position}: unknown check kind {kind!r}; known kinds: {sorted(CHECK_KINDS)}"
            )
        checks.append(Check(kind=kind, args=args, negate=negate))
    return tuple(checks)


def load_case(path: Path) -> Case:
    """Read one ``case.yaml``. Raises :class:`CaseError` with the offending field named."""
    where = str(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise CaseError(f"{where}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: expected a mapping at the top level")
    _reject_unknown(raw, _TOP_LEVEL, where)

    declared = str(_require(raw, "schema_version", where))
    if declared.split(".", 1)[0] != SCHEMA_VERSION:
        raise CaseError(
            f"{where}: schema_version {declared!r} is not supported "
            f"(this build reads major version {SCHEMA_VERSION})"
        )

    incident_raw = _require(raw, "incident", where)
    if not isinstance(incident_raw, dict):
        raise CaseError(f"{where}: 'incident' must be a mapping")
    _reject_unknown(incident_raw, _INCIDENT_KEYS, f"{where}.incident")
    incident = Incident(
        what=str(_require(incident_raw, "what", f"{where}.incident")),
        where=str(_require(incident_raw, "where", f"{where}.incident")),
    )

    ablate_raw = raw.get("ablate") or {}
    if not isinstance(ablate_raw, dict):
        raise CaseError(f"{where}: 'ablate' must be a mapping")
    _reject_unknown(ablate_raw, _ABLATE_KEYS, f"{where}.ablate")
    sections = tuple(
        SectionRef(file=str(item["file"]), heading=str(item["heading"]))
        for item in ablate_raw.get("remove_sections") or []
    )
    lines = tuple(
        LineRef(file=str(item["file"]), pattern=str(item["pattern"]))
        for item in ablate_raw.get("remove_lines") or []
    )
    if not sections and not lines:
        raise CaseError(f"{where}: 'ablate' removes nothing, so both arms would be identical")

    fixture_raw = raw.get("fixture") or {}
    if not isinstance(fixture_raw, dict):
        raise CaseError(f"{where}: 'fixture' must be a mapping")
    _reject_unknown(fixture_raw, _FIXTURE_KEYS, f"{where}.fixture")
    fixture = Fixture(
        files={str(k): str(v) for k, v in (fixture_raw.get("files") or {}).items()},
        uncommitted={str(k): str(v) for k, v in (fixture_raw.get("uncommitted") or {}).items()},
        git=tuple(tuple(str(part) for part in cmd) for cmd in fixture_raw.get("git") or []),
        worktrees=tuple(dict(item) for item in fixture_raw.get("worktrees") or []),
    )

    residual = tuple(str(p) for p in raw.get("residual_forbidden") or ())
    if not residual:
        raise CaseError(
            f"{where}: 'residual_forbidden' is required -- without it nothing proves the "
            "ablated arm stopped stating the rule"
        )

    violated = _parse_checks(raw.get("violated_when"), f"{where}.violated_when")
    if not violated:
        raise CaseError(f"{where}: 'violated_when' must name at least one check")

    return Case(
        name=str(_require(raw, "name", where)),
        path=path,
        rule=str(_require(raw, "rule", where)),
        home=str(_require(raw, "home", where)),
        incident=incident,
        prompt=str(_require(raw, "prompt", where)).strip(),
        ablation=AblationSpec(sections=sections, lines=lines),
        residual_forbidden=residual,
        fixture=fixture,
        compliant_when=_parse_checks(raw.get("compliant_when"), f"{where}.compliant_when"),
        violated_when=violated,
        tags=tuple(str(t) for t in raw.get("tags") or ()),
        runs=int(raw.get("runs", DEFAULT_RUNS)),
        max_turns=int(raw.get("max_turns", DEFAULT_MAX_TURNS)),
        timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        notes=str(raw.get("notes", "")),
    )


def load_cases(
    suite_dir: Path,
    *,
    name_glob: Optional[str] = None,
    tags: Sequence[str] = (),
) -> List[Case]:
    """Every case under ``suite_dir``, filtered by name glob and tags, in name order.

    A malformed case raises rather than being skipped. A suite that silently drops the one
    case somebody just broke reports a smaller sweep as a complete one.
    """
    cases = [load_case(path) for path in sorted(suite_dir.glob("*/case.yaml"))]
    if name_glob:
        cases = [c for c in cases if fnmatch.fnmatch(c.name, name_glob)]
    if tags:
        wanted = set(tags)
        cases = [c for c in cases if wanted & set(c.tags)]
    return sorted(cases, key=lambda c: c.name)


def duplicate_names(cases: Sequence[Case]) -> List[str]:
    """Names claimed by more than one case. The report keys on the name, so these collide."""
    seen: Dict[str, int] = {}
    for case in cases:
        seen[case.name] = seen.get(case.name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)
