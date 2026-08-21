"""Which gate stages a change since the last verified commit could possibly affect.

This is the machinery behind ``scripts/check.py --since-gate``, and it exists because of
one worked example, recorded in task-221. A branch carrying Python and docs changes was
rebased onto ``main``; the rebase brought in **one task YAML** -- a record correction
committed minutes earlier -- and the full gate was then run again from the top to
re-establish something that could not have changed.

The rule ``ENGINEERING.md`` states is emphatic and correct: the unqualified
``scripts/check.py`` is what the commit rule means, and ``--only``/``--from`` are for
iterating on a failure, never for committing. Anything here is an exception to that, and
exceptions are how such a rule erodes -- "except after a harmless rebase" becomes
"except when I judged it unnecessary" within a month, judged by the party who wants to
skip the wait. So three properties are load-bearing, and none of them is politeness:

**The tool derives the answer; nobody asserts it.** The input is a diff against a commit
the gate itself verified and wrote a receipt for. There is no flag that means "trust me".

**The table is default-deny.** ``CLASSES`` lists path patterns that map to a *reduced*
set of stages. A path matching none of them selects every stage. So the failure mode of
an incomplete table is a gate that runs too much, which costs a minute. The failure mode
of the opposite arrangement is a stage that silently stops running.

**A reduced run says so, at both ends, and names its evidence.** The receipt commit,
every changed path, the class each was matched by, and every skipped stage with the
reason. A third party reading the output can disagree with the claim, which is exactly
what ``PARTIAL RUN`` already achieves for ``--only``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

RECEIPT_FILENAME = "agentjobs-gate-receipt.json"
"""Kept in the git directory, not the work tree.

A worktree has its own git directory, so each checkout gets its own receipt -- which is
right, because each verifies its own branch. It is also outside the tree the gate is
verifying, so a receipt can never itself be a change the next run has to classify.
"""

CORPUS_STAGES = ("pytest",)
"""What a task-record change can move.

Not "nothing", which is the tempting answer and the wrong one. ``tests/test_validate.py
::TestRealCorpus`` loads this repository's own task files and asserts none is unreadable
or points at nothing, so a task YAML genuinely can turn the suite red -- and it is the
one stage whose inputs are not bounded by the diff. It runs. Nothing else reads
``tasks/``: Black, Ruff and MyPy do not see YAML, and no frontend stage reads the corpus
off disk (the React app asks the API, which the e2e server seeds itself).
"""

DOCS_STAGES = ("pytest",)
"""What a prose change can move.

``tests/test_documentation_contract.py`` asserts on the content of these files, so a
docs edit can fail the suite. Nothing else reads them.
"""


@dataclass(frozen=True)
class Class:
    """One family of paths, and the stages a change to it can affect."""

    pattern: str
    stages: Tuple[str, ...]
    why: str


CLASSES: Tuple[Class, ...] = (
    Class("tasks/*", CORPUS_STAGES, "task records; the live corpus TestRealCorpus reads"),
    Class("docs/*", DOCS_STAGES, "prose; the documentation contract tests read it"),
    Class("*.md", DOCS_STAGES, "prose; the documentation contract tests read it"),
)
"""Deliberately three entries.

Every candidate fourth entry was measured against what it would save and dropped.
``frontend/*`` would spare Black, Ruff and MyPy -- 2.1 seconds. ``assets/*`` would spare
about the same. Neither is worth a row in a table whose whole risk is being wrong, and a
table that grows for savings of that size is one nobody audits. Add an entry only when
it skips a stage measured in minutes, and say in the docstring what reads the paths.
"""


def classify(path: str) -> Optional[Class]:
    """The class a path belongs to, or None when nothing claims it.

    Matching is on the forward-slash path git reports, so it behaves the same on
    Windows. ``fnmatch`` treats ``*`` as matching separators too, which is what is wanted
    here: ``tasks/*`` should claim ``tasks/agentjobs/task-233.yaml``.
    """
    for candidate in CLASSES:
        if fnmatch(path, candidate.pattern):
            return candidate
    return None


def stages_for(paths: Sequence[str], every: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
    """The stages these paths can affect, and why each path selected what it did.

    Returns the stage names in ``every``'s order, plus a path -> reason mapping for the
    report. An unclassified path selects everything and says so; that is the default-deny
    property, and it is what makes an incomplete ``CLASSES`` table safe.
    """
    selected: set[str] = set()
    reasons: Dict[str, str] = {}
    for path in paths:
        matched = classify(path)
        if matched is None:
            selected.update(every)
            reasons[path] = "unclassified, so every stage"
        else:
            selected.update(matched.stages)
            reasons[path] = f"{matched.why} -> {', '.join(matched.stages)}"
    return [name for name in every if name in selected], reasons


# ----- talking to git ---------------------------------------------------------


def _git(root: Path, *args: str) -> Optional[str]:
    """Run a read-only git command, or return None if git cannot answer.

    Every caller treats None as "fall back to the full gate", so a repository git will
    not talk about never produces a reduced run.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def receipt_path(root: Path) -> Optional[Path]:
    """Where this checkout's receipt lives, inside its own git directory."""
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    if not git_dir:
        return None
    return Path(git_dir.strip()) / RECEIPT_FILENAME


def read_receipt(root: Path) -> Optional[Dict[str, object]]:
    """The last commit a green gate in this checkout attested to, if any."""
    path = receipt_path(root)
    if path is None or not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) and loaded.get("commit") else None


def head_commit(root: Path) -> Optional[str]:
    output = _git(root, "rev-parse", "HEAD")
    return output.strip() if output else None


def tree_is_clean(root: Path) -> bool:
    """No staged, unstaged or untracked changes.

    A receipt names a commit, so it may only be written when the working tree *is* that
    commit. Issuing one from a dirty tree would attest to code that exists nowhere.
    """
    output = _git(root, "status", "--porcelain")
    return output is not None and not output.strip()


def changed_since(root: Path, commit: str) -> Optional[List[str]]:
    """Every path in the working tree that differs from ``commit``, untracked included.

    Working tree rather than ``HEAD``: the question is what the gate is about to verify,
    which includes edits nobody has committed. An untracked file counts -- it is source
    the next commit will carry.
    """
    if _git(root, "cat-file", "-e", f"{commit}^{{commit}}") is None:
        return None
    tracked = _git(root, "diff", "--name-only", commit)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    paths = {line.strip() for line in (tracked + "\n" + untracked).splitlines() if line.strip()}
    return sorted(paths)


def write_receipt(root: Path, commit: str, *, basis: Optional[str]) -> Optional[Path]:
    """Attest that the gate is satisfied at ``commit``.

    ``basis`` records the receipt a reduced run derived its authority from, so a chain of
    them is auditable rather than anonymous. A full run has no basis; it verified
    everything itself.
    """
    path = receipt_path(root)
    if path is None:
        return None
    payload = {"commit": commit, "basis": basis}
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


# ----- the report -------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What a ``--since-gate`` run decided, and everything needed to argue with it."""

    stages: Optional[List[str]]
    """The stage names to run. ``None`` means the decision could not be made: run all."""

    commit: Optional[str]
    paths: List[str]
    reasons: Dict[str, str]
    refusal: Optional[str]

    @property
    def reduced(self) -> bool:
        return self.stages is not None


def resolve(root: Path, every: Sequence[str]) -> Scope:
    """Decide what a ``--since-gate`` run should do, from git and the receipt alone."""
    receipt = read_receipt(root)
    if receipt is None:
        return Scope(None, None, [], {}, "no gate receipt in this checkout")
    commit = str(receipt["commit"])
    paths = changed_since(root, commit)
    if paths is None:
        return Scope(None, commit, [], {}, f"cannot diff against the receipt commit {commit[:8]}")
    stages, reasons = stages_for(paths, every)
    return Scope(stages, commit, paths, reasons, None)


def render(scope: Scope, every: Sequence[str]) -> str:
    """The evidence for a reduced run, printed whether or not anyone asked.

    A reduced run is a claim about what could not have changed. The claim is only
    checkable if the reader can see the commit it rests on, the paths it examined, and
    the rule each path was matched by -- so all three are printed, and the banner says
    outright that this is not the gate.
    """
    if not scope.reduced:
        return (
            f"FULL GATE: --since-gate could not narrow anything ({scope.refusal}).\n"
            "Running every stage."
        )
    short = (scope.commit or "")[:8]
    if not scope.paths:
        return (
            f"NOTHING CHANGED since the gate verified {short}.\n"
            "The working tree is identical to the commit that last passed every stage."
        )
    lines = [
        f"NECESSITY RUN: {len(scope.stages or [])} of {len(every)} stages, "
        f"derived from the diff against {short} -- the commit this checkout's gate "
        "last verified in full.",
        "This is not the gate. It asserts that the stages below are the only ones the "
        "changes since then can reach.",
        "",
        f"Changed since {short} ({len(scope.paths)} path"
        f"{'' if len(scope.paths) == 1 else 's'}):",
    ]
    for path in scope.paths:
        lines.append(f"  {path}  --  {scope.reasons.get(path, '?')}")
    skipped = [name for name in every if name not in (scope.stages or [])]
    if skipped:
        lines += ["", f"Skipped: {', '.join(skipped)}. Nothing changed that they read."]
    return "\n".join(lines)
