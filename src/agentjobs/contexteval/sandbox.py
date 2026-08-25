"""Building the scratch world one arm of one scenario runs in.

Task-304's constraint is unambiguous: an eval run must not touch the live backlog, the
shared clone, or ``main``, and no real task record may be used as a fixture. Two mechanisms
enforce that, and neither is a prompt asking the agent nicely.

**Location.** The sandbox is a fresh directory under the operating system's temp area. It
is not under ``C:/projects``, so the real ``CLAUDE.md`` -> ``AGENTS.md`` chain above the
projects directory does not load, and Claude's per-project auto-memory -- keyed on the
project directory, and which audit 1 found restates at least eight bundle rules -- does not
exist for that path. Both were probed on 2026-08-25 rather than assumed.

**A guard hook.** ``.claude/settings.json`` in the sandbox registers a ``PreToolUse`` hook
that denies any tool call whose input names the real workspace. It is a hook and not a
permission rule because the sessions run at ``bypassPermissions``, where permission rules
do not apply and hooks still do -- probed the same day: a run told to ``ls C:/projects`` was
refused, and the refusal came back in the result JSON's ``permission_denials``. That list
is carried into the report, so a run that reached for the real workspace is visible rather
than merely stopped.

The clone itself is a plain git repository with the arm's bundle at its root, a throwaway
project registered at ``.agentjobs/config.yaml``, and whatever files the scenario asked for.
``AGENTJOBS_HOME`` is pointed inside the sandbox as well, so the ``agentjobs`` CLI -- which
the bundle tells the agent to use -- resolves an empty registry instead of the live one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from agentjobs.contexteval.cases import Case

GUARD_PATTERNS: tuple = (
    r"c:[\\/]projects",
    r"/c/projects",
    r"[\\/]\.agentjobs[\\/]",
    r"~[\\/]\.agentjobs",
    r":8876\b",
    r":8765\b",
)
"""Everything a sandbox session must not reach.

The two ports are the live dashboard and the CLI default. A scenario about restarting a
server has no business talking to either, and an agent that has read the bundle knows both
numbers by heart.
"""

_GUARD_SOURCE = '''"""Refuse any tool call that reaches outside the eval sandbox.

Registered as a PreToolUse hook by the sandbox's settings.json. Hooks fire even at
permission-mode bypassPermissions, which is why the containment lives here rather than in
a permissions allow-list.
"""

import json
import re
import sys

PATTERNS = __PATTERNS__

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return
    blob = json.dumps(payload.get("tool_input", {}), ensure_ascii=False)
    for pattern in PATTERNS:
        if re.search(pattern, blob, re.IGNORECASE):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "context-eval sandbox: the real workspace is out of bounds for "
                        "this session. Everything you need is inside this directory."
                    ),
                }
            }))
            return
    print(json.dumps({}))

main()
'''


@dataclass
class Sandbox:
    """A built scratch world, and the facts a run needs about it."""

    root: Path
    clone: Path
    cwd: Path
    base_commit: str
    env: Dict[str, str]

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _git(clone: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def write_guard(root: Path, patterns: Sequence[str] = GUARD_PATTERNS) -> Path:
    """Write the guard script and the settings that register it. Returns the script path."""
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    guard = claude_dir / "sandbox_guard.py"
    guard.write_text(
        _GUARD_SOURCE.replace("__PATTERNS__", json.dumps(list(patterns))), encoding="utf-8"
    )
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'python "{guard.as_posix()}"',
                        }
                    ],
                }
            ]
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return guard


def build(
    case: Case,
    bundle_files: Mapping[str, str],
    *,
    parent: Optional[Path] = None,
    guard_patterns: Sequence[str] = GUARD_PATTERNS,
) -> Sandbox:
    """Create the sandbox for one arm of one scenario and return it ready to run in.

    ``bundle_files`` is the arm's rendered bundle -- the control arm passes the repository's
    own text, the treatment arm passes it with the rule cut out. Nothing else differs
    between the two, which is the whole basis for attributing a difference in behaviour to
    the removal.
    """
    root = Path(tempfile.mkdtemp(prefix="ajctxeval-", dir=str(parent) if parent else None))
    clone = root / "clone"
    clone.mkdir(parents=True)

    for name, text in bundle_files.items():
        _write(clone / name, text)

    _write(
        clone / ".agentjobs" / "config.yaml",
        "project: demo\ntasks_directory: tasks/demo\n",
    )
    for relative, text in case.fixture.files.items():
        _write(clone / relative, text)

    _git(clone, "init", "-q", "-b", "main")
    _git(clone, "config", "user.email", "eval@example.invalid")
    _git(clone, "config", "user.name", "Context Eval")
    _git(clone, "config", "commit.gpgsign", "false")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "chore: scratch fixture")

    base = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    ).stdout.strip()

    # Uncommitted files land after the initial commit on purpose: a peer's in-flight work
    # is exactly a file that is in the tree and not in history.
    for relative, text in case.fixture.uncommitted.items():
        _write(clone / relative, text)

    for command in case.fixture.git:
        _git(clone, *command)

    for spec in case.fixture.worktrees:
        target = root / "worktrees" / str(spec["name"])
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(clone, "worktree", "add", "-q", "-b", str(spec["branch"]), str(target))
        seeded = spec.get("files") or {}
        for relative, text in seeded.items():
            _write(target / str(relative), str(text))
        if seeded:
            _git(target, "add", *[str(r) for r in seeded])
            _git(target, "commit", "-q", "-m", str(spec.get("message", "feat: work in progress")))

    write_guard(root, guard_patterns)
    # A worktree's own directory does not inherit the clone's .claude, and the session's
    # cwd is what Claude Code reads project settings from.
    if case.fixture.worktrees:
        for spec in case.fixture.worktrees:
            shutil.copytree(root / ".claude", root / "worktrees" / spec["name"] / ".claude")
    shutil.copytree(root / ".claude", clone / ".claude", dirs_exist_ok=True)

    env = {
        "AGENTJOBS_HOME": str(root / "agentjobs-home"),
        "AGENTJOBS_SKIP_SOURCE_CHECK": "1",
    }
    (root / "agentjobs-home").mkdir(parents=True, exist_ok=True)

    cwd = root / case.cwd
    return Sandbox(root=root, clone=clone, cwd=cwd, base_commit=base, env=env)


def describe(sandbox: Sandbox) -> List[str]:
    """A short human-readable dump of the end state, for the per-run record."""
    lines: List[str] = []
    for args in (
        ("branch", "-vv"),
        ("worktree", "list"),
        ("log", "--all", "--oneline", "--decorate", "-n", "20"),
        ("status", "--short"),
    ):
        done = subprocess.run(
            ["git", "-C", str(sandbox.clone), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines.append(f"$ git {' '.join(args)}")
        lines.extend((done.stdout or done.stderr or "").rstrip().splitlines())
        lines.append("")
    return lines
