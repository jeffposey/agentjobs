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

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
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


def dirty_paths(root: Path) -> List[Tuple[str, str]]:
    """Every path that stops this tree being a commit, with what is wrong with each.

    The receipt rule -- a green full gate on a *clean* tree earns one -- is stated in
    ``issue_receipt`` and was, until task-339, enforced silently. task-336's run left
    two untracked sandbox files behind, so its fourth green gate wrote no receipt, and
    the ``--since-gate`` seven minutes later found nothing to narrow against and fell
    back to all ten stages: ten more minutes to re-establish what a receipt would have
    settled instantly. Nothing anywhere named the two files.

    So the paths are the finding, and both callers print them: the run that failed to
    earn a receipt, and the reduced run that went looking for one.
    """
    output = _git(root, "status", "--porcelain")
    if output is None:
        return []
    entries: List[Tuple[str, str]] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if not path:
            continue
        entries.append((path, "untracked" if code == "??" else f"{code.strip()} in git status"))
    return entries


def render_dirty(entries: Sequence[Tuple[str, str]]) -> List[str]:
    """The dirty paths as printable lines, or nothing at all when the tree is clean."""
    if not entries:
        return []
    lines = [
        f"These {len(entries)} path{'' if len(entries) == 1 else 's'} are what stop this "
        "tree being a commit, and so what stops a receipt:"
    ]
    lines += [f"  {path}  --  {why}" for path, why in entries]
    lines.append("Commit or remove them, then the gate you run next earns a receipt.")
    return lines


def tree_fingerprint(root: Path) -> Optional[str]:
    """A short digest of exactly what the gate is about to verify, commits included.

    ``HEAD`` alone is not it: the gate verifies the working tree, and task-336 ran two
    full gates back to back over one uncommitted change and then two more over the
    committed form of the same code. What identifies "this exact tree" is the commit,
    plus the patch against it, plus whatever is untracked -- content and all, since an
    untracked file is source the next commit will carry.

    Returns None when git cannot answer, which every caller reads as "cannot tell",
    never as "unchanged". A fingerprint that guesses would be worse than none: the whole
    use of it is to say *nothing has changed since that green run*.
    """
    commit = head_commit(root)
    if commit is None:
        return None
    patch = _git(root, "diff", "HEAD")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if patch is None or untracked is None:
        return None
    digest = hashlib.sha256()
    digest.update(commit.encode("utf-8"))
    digest.update(patch.encode("utf-8", "replace"))
    for name in sorted(line.strip() for line in untracked.splitlines() if line.strip()):
        digest.update(name.encode("utf-8", "replace"))
        try:
            digest.update(hashlib.sha256((root / name).read_bytes()).hexdigest().encode("ascii"))
        except OSError:
            # A path git lists and we cannot read is still a difference; record that it
            # was there and that we could not see into it, rather than ignoring it.
            digest.update(b"unreadable")
    return digest.hexdigest()[:16]


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

    blocking: List[Tuple[str, str]] = field(default_factory=list)
    """The dirty paths, when the refusal was that no receipt exists (task-339).

    A refusal that names only itself leaves the reader with a ten-minute gate and no
    idea what to do about it next time. These are the paths that stopped the last green
    gate earning a receipt, and they are almost always a couple of scratch files.
    """

    @property
    def reduced(self) -> bool:
        return self.stages is not None


def resolve(root: Path, every: Sequence[str]) -> Scope:
    """Decide what a ``--since-gate`` run should do, from git and the receipt alone."""
    receipt = read_receipt(root)
    if receipt is None:
        return Scope(
            None,
            None,
            [],
            {},
            "no gate receipt in this checkout",
            blocking=dirty_paths(root),
        )
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
        lines = [
            f"FULL GATE: --since-gate could not narrow anything ({scope.refusal}).",
            "Running every stage.",
        ]
        lines += render_dirty(scope.blocking)
        return "\n".join(lines)
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
