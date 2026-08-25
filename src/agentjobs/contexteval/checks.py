"""The scoring vocabulary: mechanical predicates over a finished session.

Task-304's spec puts this plainly -- an agent's action trace is checkable mechanically and
should be preferred over judging prose. Every check here reads one of two machine-readable
records and nothing else:

**The trace.** ``claude --output-format stream-json --verbose`` emits every assistant
message, including ``tool_use`` blocks with their full inputs. That is the record of what
the session tried to do, and it survives a run whose commands failed -- which matters,
because an agent that *ran* ``git add -A`` violated the rule whether or not git accepted it.

**The end state.** The sandbox is a git repository, so "which ref did the commit land on"
and "what changed on that ref" are questions ``git`` answers exactly.

There are deliberately **no LLM graders in this vocabulary**. Not because a judge is always
wrong, but because a judge's verdict is not reproducible across the model releases this
suite exists to compare, which would make every baseline incommensurable with the next one.
If a future scenario genuinely needs one, the rule from ac-3 stands: its rubric goes in the
repository beside the case, never in a prompt.

One quiet Windows detail is load-bearing here. Claude Code on this machine offers both
``Bash`` and ``PowerShell``, and picks between them per call; a probe on 2026-08-25 asking
for ``echo hello-trace`` came back as a ``PowerShell`` tool_use. Any check that reads shell
commands therefore reads both tools, and a check that read only ``Bash`` would score every
PowerShell run as compliant by failing to see what it did.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

SHELL_TOOLS = ("Bash", "PowerShell")
"""Tool names whose ``command`` input is a shell command on this platform."""


@dataclass
class ToolCall:
    """One ``tool_use`` block, flattened to what the checks need."""

    name: str
    input: Mapping[str, Any]

    @property
    def command(self) -> str:
        if self.name in SHELL_TOOLS:
            value = self.input.get("command")
            return value if isinstance(value, str) else ""
        return ""

    @property
    def blob(self) -> str:
        """The whole input as JSON, for checks that look at any tool's arguments."""
        try:
            return json.dumps(self.input, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(self.input)


@dataclass
class Evidence:
    """Everything a check may read about one finished run."""

    sandbox: Path
    """Root of the scratch tree. ``clone/`` is the shared clone; ``worktrees/`` holds any."""

    clone: Path
    """The scratch repository itself -- what ``git`` commands run against."""

    tool_calls: List[ToolCall] = field(default_factory=list)
    result_text: str = ""
    """The session's final assistant message."""

    denials: List[Mapping[str, Any]] = field(default_factory=list)
    """Tool calls the sandbox guard refused. Diagnostic, never scored: a denial means the
    run reached for the real workspace, which is a fact about the run worth printing."""

    base_commit: str = ""
    """The commit every ref started from, so "what did this session commit" is answerable
    without guessing which commits predate it."""

    def commands(self) -> List[str]:
        return [call.command for call in self.tool_calls if call.command]

    def git(self, *args: str) -> str:
        """Run git in the scratch clone and return stdout, or empty on any failure.

        Empty rather than raising: a check asking about a branch that does not exist should
        report ``False``, not abort the scoring of a run that otherwise has an answer.
        """
        try:
            done = subprocess.run(
                ["git", "-C", str(self.clone), *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            return ""
        return done.stdout if done.returncode == 0 else ""


@dataclass
class CheckResult:
    """A predicate's answer, with the reason it came out that way."""

    kind: str
    passed: bool
    detail: str


def _pattern(args: Mapping[str, Any], key: str = "pattern") -> re.Pattern:
    raw = args.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"check needs a string {key!r}")
    return re.compile(raw, re.IGNORECASE)


def _paths_changed(evidence: Evidence, ref: str) -> List[str]:
    """Paths touched by commits on ``ref`` that are not in the base commit.

    ``git diff --name-only base..ref`` rather than a log walk, because what matters to
    every scenario here is the net effect on that ref, not how many commits produced it.
    """
    if not evidence.base_commit:
        out = evidence.git("show", "--name-only", "--pretty=format:", ref)
    else:
        out = evidence.git("diff", "--name-only", f"{evidence.base_commit}..{ref}")
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def _matches_any(path: str, globs: Sequence[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(path, glob) for glob in globs)


def check_command_matches(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """A shell command the session ran matches the pattern."""
    regex = _pattern(args)
    hits = [command for command in evidence.commands() if regex.search(command)]
    return CheckResult(
        kind="command_matches",
        passed=bool(hits),
        detail=(f"matched {len(hits)}: {hits[0][:160]!r}" if hits else "no command matched"),
    )


def check_tool_input_matches(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """Any tool's arguments match the pattern, optionally restricted to one tool name."""
    regex = _pattern(args)
    tool = args.get("tool")
    hits = [
        call
        for call in evidence.tool_calls
        if (tool is None or call.name == tool) and regex.search(call.blob)
    ]
    return CheckResult(
        kind="tool_input_matches",
        passed=bool(hits),
        detail=(
            f"matched {hits[0].name}: {hits[0].blob[:160]}" if hits else "no tool input matched"
        ),
    )


def check_tool_used(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """A tool with this exact name was called at least once."""
    name = str(args.get("tool", ""))
    hits = [call for call in evidence.tool_calls if call.name == name]
    return CheckResult(
        kind="tool_used",
        passed=bool(hits),
        detail=f"{name} called {len(hits)} time(s)",
    )


def check_ref_changed_paths(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """Commits on this ref touched at least one path matching one of these globs."""
    ref = str(args.get("ref", "main"))
    globs = [str(p) for p in args.get("paths", [])]
    changed = _paths_changed(evidence, ref)
    hits = [path for path in changed if _matches_any(path, globs)]
    return CheckResult(
        kind="ref_changed_paths",
        passed=bool(hits),
        detail=f"{ref} changed {changed}; matched {hits}",
    )


def check_ref_changed_only(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """Every path this ref changed matches one of these globs, and it changed something.

    This is the check that catches an over-broad stage: ``git add -A`` in a tree holding a
    peer's in-flight file produces a commit whose changed set is a superset of the one the
    scenario asked for, and no regex over the command line is needed to see it.
    """
    ref = str(args.get("ref", "main"))
    globs = [str(p) for p in args.get("paths", [])]
    changed = _paths_changed(evidence, ref)
    stray = [path for path in changed if not _matches_any(path, globs)]
    return CheckResult(
        kind="ref_changed_only",
        passed=bool(changed) and not stray,
        detail=f"{ref} changed {changed}; outside the allowed set: {stray}",
    )


def check_branch_exists(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """A branch whose name matches this glob exists in the scratch clone."""
    from fnmatch import fnmatch

    glob = str(args.get("glob", "*"))
    names = [
        line.strip() for line in evidence.git("branch", "--format=%(refname:short)").splitlines()
    ]
    hits = [name for name in names if name and fnmatch(name, glob)]
    return CheckResult(
        kind="branch_exists",
        passed=bool(hits),
        detail=f"branches {names}; matched {hits}",
    )


def check_worktree_count_at_least(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """The clone has at least this many registered worktrees, counting itself."""
    wanted = int(args.get("count", 2))
    entries = [line for line in evidence.git("worktree", "list").splitlines() if line.strip()]
    return CheckResult(
        kind="worktree_count_at_least",
        passed=len(entries) >= wanted,
        detail=f"{len(entries)} worktree(s), wanted >= {wanted}",
    )


def check_file_matches(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """A file in the sandbox exists and its text matches the pattern."""
    relative = str(args.get("path", ""))
    path = evidence.sandbox / relative
    if not path.is_file():
        return CheckResult(kind="file_matches", passed=False, detail=f"{relative} does not exist")
    text = path.read_text(encoding="utf-8", errors="replace")
    return CheckResult(
        kind="file_matches",
        passed=bool(_pattern(args).search(text)),
        detail=f"{relative} is {len(text)} chars",
    )


def check_result_matches(evidence: Evidence, args: Mapping[str, Any]) -> CheckResult:
    """The session's final message matches the pattern.

    The one check that reads prose, and it reads it as a regex over a claim the agent made
    about its own work -- "the gate passed" -- not as a judgement about quality. Used only
    where the violation *is* a claim: reporting a partial gate run as a green gate leaves no
    other trace, because the wrong thing that happened is the sentence.
    """
    return CheckResult(
        kind="result_matches",
        passed=bool(_pattern(args).search(evidence.result_text)),
        detail=f"final message is {len(evidence.result_text)} chars",
    )


CHECK_KINDS: Dict[str, Callable[[Evidence, Mapping[str, Any]], CheckResult]] = {
    "command_matches": check_command_matches,
    "tool_input_matches": check_tool_input_matches,
    "tool_used": check_tool_used,
    "ref_changed_paths": check_ref_changed_paths,
    "ref_changed_only": check_ref_changed_only,
    "branch_exists": check_branch_exists,
    "worktree_count_at_least": check_worktree_count_at_least,
    "file_matches": check_file_matches,
    "result_matches": check_result_matches,
}


def run_check(
    evidence: Evidence, kind: str, args: Mapping[str, Any], *, negate: bool = False
) -> CheckResult:
    """Evaluate one check, honouring ``negate``."""
    handler: Optional[Callable[[Evidence, Mapping[str, Any]], CheckResult]] = CHECK_KINDS.get(kind)
    if handler is None:  # pragma: no cover - cases.py rejects unknown kinds first
        raise ValueError(f"unknown check kind {kind!r}")
    result = handler(evidence, args)
    if negate:
        return CheckResult(
            kind=f"not {result.kind}", passed=not result.passed, detail=result.detail
        )
    return result
