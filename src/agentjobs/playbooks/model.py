"""The playbook contract: markdown with YAML frontmatter, and what validates it.

``docs/playbooks-design.md`` §3.2 is the specification. The machine-read half of a
playbook is its frontmatter; the brief is the markdown body and is **opaque prose** --
nothing here parses it, evaluates it, or acts on it.

Two properties are worth stating because they are load-bearing rather than incidental:

**Validation reports findings, it does not raise on the first problem.** A playbook
directory is authored by hand and a reader who is told about one mistake at a time
fixes them one round-trip at a time. Every finding names the file, because the caller
that surfaces them (``playbook list``, the REST collection, ``playbooks_list``) is
listing several files at once and "name must match the filename" is unactionable
without one.

**Unknown frontmatter keys are refused.** ``run_taks:`` under a tolerant reader is a
silently ignored contract, which is the failure mode ``StrictModel`` exists to stop for
task records; a playbook's frontmatter has the same authored-by-hand exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models_v2 import Priority, ValueEnum

FRONTMATTER_FENCE = "---"
"""The delimiter a playbook's frontmatter opens and closes with."""

PLAYBOOK_SUFFIX = ".md"

MANAGER_VERBS: Tuple[str, ...] = (
    "create",
    "promote",
    "claim",
    "release",
    "handoff",
    "close",
    "log",
    "update",
    "queue_move",
)
"""The verbs a playbook may declare in ``verbs:``.

Spelling only. §6.2 and decision P5 are explicit that the verb list is a *declared
contract audited from the record*, not a per-run mechanical gate, and nothing here
moves that line: refusing ``clsoe`` is a typo check, and it exists because a contract
nobody can match against the log is not auditable at all.
"""


class PlaybookTarget(ValueEnum):
    """What a run is aimed at (§3.2)."""

    PROJECT = "project"
    TASK = "task"


class PlaybookDifficulty(ValueEnum):
    """The capability the work needs, in the task-156 vocabulary (§7.2)."""

    ROUTINE = "routine"
    STANDARD = "standard"
    HARD = "hard"


class _Strict(BaseModel):
    """Frontmatter models forbid unknown keys, for the reason in the module docstring."""

    model_config = ConfigDict(extra="forbid")


class PlaybookGate(_Strict):
    """Where a run must stop for a human, stated declaratively (§3.2)."""

    before: str = Field(..., min_length=1, description="The verb this gate stands in front of.")
    what: str = Field(..., min_length=1, description="What must be true before it is passed.")


class PlaybookAcceptance(_Strict):
    """One acceptance criterion the run task is created with."""

    text: str = Field(..., min_length=1)
    verify: Optional[str] = Field(
        default=None, description="Optional machine-checkable hint, mirroring the task field."
    )


class PlaybookRunTask(_Strict):
    """Defaults for the run task a ``target: project`` run creates (§3.2).

    Nothing in this module creates that task -- instantiation is task-215. These are
    the values it will read.
    """

    title: str = Field(..., min_length=1)
    category: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Project taxonomy for the run task. Not validated against the project's "
            "configured categories here: a playbook is read without reference to any "
            "one project's config, and the manager validates the category at creation."
        ),
    )
    priority: Optional[Priority] = None
    tags: List[str] = Field(default_factory=list)
    acceptance: List[PlaybookAcceptance] = Field(default_factory=list)


class PlaybookContract(_Strict):
    """A playbook's frontmatter: everything a machine reads about it."""

    name: str = Field(..., min_length=1, description="Must equal the filename stem.")
    description: str = Field(..., min_length=1)
    target: PlaybookTarget
    difficulty: PlaybookDifficulty
    verbs: List[str] = Field(default_factory=list)
    gates: List[PlaybookGate] = Field(default_factory=list)
    run_task: Optional[PlaybookRunTask] = None


@dataclass(frozen=True)
class Playbook:
    """One loaded playbook: its contract, its brief, and where it was read from."""

    contract: PlaybookContract
    body: str
    path: Path
    source: str = ""
    """The whole file, frontmatter included, exactly as it was parsed.

    Carried so a run can hash **what it read** rather than re-opening the file to hash
    it afterwards (see :mod:`agentjobs.playbooks.pointer`). Re-reading opens a window in
    which the file changes between the parse and the hash, and the record would then
    pin a brief no run used. Defaulted so a caller building one by hand -- a test, a
    future editor preview -- is unaffected.
    """

    @property
    def name(self) -> str:
        """The playbook's name, which is also its filename stem."""
        return self.contract.name

    @property
    def description(self) -> str:
        """The one-line answer ``playbook list`` prints."""
        return self.contract.description


@dataclass(frozen=True)
class PlaybookFinding:
    """One reason a file is not a valid playbook.

    ``filename`` rather than the whole path: a listing shows several of these at once
    and the directory is the same for all of them, so the stem is what distinguishes
    them. ``path`` is carried for a caller that wants to print it in full.
    """

    path: Path
    field: Optional[str]
    message: str

    @property
    def filename(self) -> str:
        """The file's name, which is what a listing identifies it by."""
        return self.path.name

    def render(self) -> str:
        """``groom.md: name -- must equal the filename stem``, for a terminal."""
        where = f"{self.filename}: " if self.filename else ""
        field = f"{self.field} -- " if self.field else ""
        return f"{where}{field}{self.message}"


class PlaybookError(Exception):
    """Raised when a file cannot be read as a playbook.

    Carries every finding rather than only the first, so one round-trip fixes one
    file. ``str(exc)`` is the findings joined, because the CLI prints exceptions.
    """

    def __init__(self, findings: List[PlaybookFinding]) -> None:
        self.findings = findings
        super().__init__("; ".join(finding.render() for finding in findings))


def split_frontmatter(text: str) -> Tuple[str, str]:
    """Split a playbook file into its raw frontmatter and its body.

    The file must open with a ``---`` fence on its own first line and close with
    another. Raises :class:`ValueError` with a reader-facing message otherwise; the
    caller turns that into a finding naming the file.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalised.startswith(FRONTMATTER_FENCE + "\n"):
        raise ValueError("the file must start with a '---' line opening the YAML frontmatter block")
    remainder = normalised[len(FRONTMATTER_FENCE) + 1 :]
    marker = f"\n{FRONTMATTER_FENCE}\n"
    end = remainder.find(marker)
    if end == -1:
        if remainder.endswith(f"\n{FRONTMATTER_FENCE}"):
            return remainder[: -(len(FRONTMATTER_FENCE) + 1)], ""
        raise ValueError("the frontmatter block is never closed by a second '---' line")
    return remainder[:end], remainder[end + len(marker) :]


def _findings_from_validation(path: Path, error: ValidationError) -> List[PlaybookFinding]:
    """Translate Pydantic's report into findings a person can act on."""
    findings: List[PlaybookFinding] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail.get("loc", ())) or None
        findings.append(
            PlaybookFinding(path=path, field=location, message=detail.get("msg", "is invalid"))
        )
    return findings


def _verb_findings(path: Path, contract: PlaybookContract) -> List[PlaybookFinding]:
    """Refuse a verb that is not a manager verb, naming the ones that are."""
    known = ", ".join(MANAGER_VERBS)
    return [
        PlaybookFinding(
            path=path,
            field=f"verbs.{index}",
            message=f"{verb!r} is not a manager verb. The verbs are: {known}.",
        )
        for index, verb in enumerate(contract.verbs)
        if verb not in MANAGER_VERBS
    ]


def parse_playbook(path: Path, text: str) -> Playbook:
    """Parse one playbook's text, raising :class:`PlaybookError` with every finding.

    ``path`` is what the findings name; it is not read, so a caller holding the text
    already (a test, a future editor preview) does not have to write a file first.
    """
    stem = path.stem
    try:
        raw_frontmatter, body = split_frontmatter(text)
    except ValueError as exc:
        raise PlaybookError([PlaybookFinding(path=path, field=None, message=str(exc))]) from exc

    try:
        loaded = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise PlaybookError(
            [PlaybookFinding(path=path, field=None, message=f"the frontmatter is not YAML: {exc}")]
        ) from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise PlaybookError(
            [PlaybookFinding(path=path, field=None, message="the frontmatter is not a mapping")]
        )

    findings: List[PlaybookFinding] = []
    contract: Optional[PlaybookContract] = None
    try:
        contract = PlaybookContract.model_validate(loaded)
    except ValidationError as exc:
        findings.extend(_findings_from_validation(path, exc))

    # The name check reads the raw mapping rather than the model, so a file with both
    # a bad enum and the wrong name reports both in one pass. Fixing one mistake per
    # round-trip is the thing findings exist to avoid.
    declared = loaded.get("name")
    if isinstance(declared, str) and declared != stem:
        findings.append(
            PlaybookFinding(
                path=path,
                field="name",
                message=(
                    f"is {declared!r} but the filename stem is {stem!r}; they must "
                    "match, because the name is how every surface addresses the file"
                ),
            )
        )
    if contract is not None:
        findings.extend(_verb_findings(path, contract))
    if findings or contract is None:
        raise PlaybookError(findings)

    return Playbook(contract=contract, body=body, path=path, source=text)


def load_playbook(path: Path) -> Playbook:
    """Read and parse one playbook file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlaybookError(
            [PlaybookFinding(path=Path(path), field=None, message=f"cannot be read: {exc}")]
        ) from exc
    return parse_playbook(Path(path), text)


def playbook_payload(playbook: Playbook, *, include_body: bool) -> Dict[str, Any]:
    """One playbook as a JSON-ready mapping, for the API and the MCP tool."""
    payload: Dict[str, Any] = playbook.contract.model_dump(mode="json", exclude_none=True)
    payload["filename"] = playbook.path.name
    if include_body:
        payload["body"] = playbook.body
    return payload
