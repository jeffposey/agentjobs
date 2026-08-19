"""A starting ``dispatch.yaml`` to show someone, and to write only when asked.

``~/.agentjobs/dispatch.yaml`` is written by hand, which is the point: it is the record
of exactly what this machine will execute, and nothing that a repository or a browser
can reach may widen it (design section 6, gate 3). The cost of that rule is that a
first-time reader faces an empty file with no shape to copy, and runner groups make the
shape bigger.

So this module holds one commented example and two ways to get at it, both of which a
human has to ask for by name:

    agentjobs dispatch example            # prints it
    agentjobs dispatch example --write    # writes it, refusing to overwrite

**Nothing calls this on its own.** AgentJobs never synthesises a dispatch config, never
adds an entry nobody typed, and never repairs one it does not like. A file that appears
by itself would defeat the gate that makes the file trustworthy in the first place, so
the writer refuses outright if anything already exists at the target path rather than
merging, backing up, or asking.

The example ships **switched off** at every level -- ``enabled: false`` and no project
entries -- so even the ``--write`` path cannot leave a machine able to dispatch that was
not able to before. Turning it on stays a deliberate edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agentjobs.dispatch.config import (
    DispatchError,
    dispatch_config_path,
)

EXAMPLE_CONFIG = """\
# ~/.agentjobs/dispatch.yaml -- machine-local. Never committed, never in a repository.
#
# This file is the complete list of what AgentJobs may execute on this machine. Nothing
# in a project can add to it, and the browser can only switch between what is here.
version: 1

# The master switch. Left off deliberately: fill in the runners below, check them with
# `agentjobs dispatch status`, and turn this on when you mean it.
enabled: false

# ----- runners: one named recipe per way of starting an agent -----------------
#
# argv is a list because there is no shell anywhere in dispatch. Placeholders are
# substituted per element, literally: {prompt} {task_id} {project_id} {project_root}
# {run_id} {agent} {api_base}.
#
# Flags are your business, not AgentJobs'. It never learns what a model is, and never
# builds a command from a label -- so whichever model or effort a runner uses is
# whatever its argv says.
#
# Two flags are NOT yours, and writing them here is a bug: AgentJobs splices the
# project's posture in front of the prompt itself, and that is where `--permission-mode`
# / `--tools` and the worktree flag `-w` come from. A second `-w` here would give one run
# two worktrees.
#
# `mode:` must match the invocation. A session is `--bg --remote-control` and returns a
# short id AgentJobs then polls; a batch run is `-p` and blocks until it exits. `-p` on a
# runner declared `mode: session` asks for print-and-exit while AgentJobs waits for a
# session id that will never come.
#
# argv is recorded verbatim in the task's dispatch entry. Secrets go in `env:`, which is
# never logged.
runners:
  claude-standard:
    argv: ["claude", "--bg", "--remote-control", "{prompt}"]
    mode: session
    actor: claude          # the identity it writes as, from the project's actors:

  claude-deep:
    argv: ["claude", "--bg", "--remote-control", "--effort", "high", "{prompt}"]
    mode: session
    actor: claude

  claude-quick:
    argv: ["claude", "--bg", "--remote-control", "--model", "haiku", "{prompt}"]
    mode: session
    actor: claude

  # Batch: bounded work that produces a report rather than a conversation. This is the
  # only mode with a spend ceiling -- `--max-budget-usd` is refused outside `--print`.
  # `--output-format stream-json` is refused without `--verbose`.
  claude-review:
    argv: ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--max-budget-usd", "5", "{prompt}"]
    mode: batch
    actor: claude

  # A second vendor goes here, written against its own CLI's flags once you have that
  # CLI installed and can check them. Nothing is shipped for you, because a flag nobody
  # ran is a flag that does not work; see the disabled member below for how to record
  # the intention in the meantime.

# ----- runner groups: which runners are interchangeable for a kind of work ----
#
# A group is an ordered list. The first member that can actually run is the one that
# runs: a member that is `enabled: false`, names a runner you have not written, or whose
# CLI is not installed is skipped, and the dispatch entry records every one of those
# decisions.
#
# Order is the whole preference mechanism. AgentJobs does not consult usage limits,
# because no installed agent CLI reports remaining headroom in any form a script can
# read -- see task-177 for what was tried.
#
# A group with no member that can run refuses the dispatch. It never substitutes a
# runner from outside the group, because that would spend money on a model nobody asked
# for.
runner_groups:
  standard:
    description: Ordinary work. What most dispatches should get.
    members:
      - runner: claude-standard
      - runner: codex
        enabled: false
        note: Second option. Write the runner once codex is installed and its flags
          checked -- a member may name a runner that does not exist yet, which is how
          you record the intention without pretending to know the command.

  deep:
    description: Architecture, review, anything worth the slower model.
    members:
      - claude-deep
      - claude-standard      # fall back rather than fail if the first is unavailable

  quick:
    description: Mechanical edits, renames, formatting.
    members:
      - claude-quick
      - claude-standard

  review:
    description: Bounded reports. Batch mode, so it has a spend ceiling and an exit code.
    members:
      - claude-review

# Used by any project that names no group of its own. Comment it out to keep every
# project on its own explicit `runner:` or `group:`.
default_group: standard

# ----- projects: which of your projects may dispatch, and against what --------
#
# Deliberately empty. Add one with `agentjobs dispatch enable <project> --group deep`,
# or write it here by hand:
#
# projects:
#   agentjobs:
#     enabled: true
#     group: standard          # or `runner: claude-standard` for a single runner
#     posture: auto            # read_only | auto | supervised | autonomous
#
#       auto        the default, and what you want. A classifier reviews each action,
#                   so the run keeps a gate and still never needs a terminal.
#       read_only   no shell at all, and no worktree. Review, triage, defect reports.
#       supervised  acceptEdits plus an allow-list of nine command prefixes. Anything
#                   outside them PARKS waiting for a human, so this only suits a run
#                   you are watching and willing to answer from your phone.
#       autonomous  bypassPermissions. No gate whatsoever. Opt in per project.
#
#     require_clean_tree: true
#     auto_dispatch: false
projects: {}

# ----- limits: caps that apply however a run was started ----------------------
limits:
  max_concurrent_runs: 1
  run_timeout_seconds: 1800      # batch runners only
  session_stale_seconds: 3600    # sessions: moves the ball, never kills
  auto:                          # these bind auto-dispatch only
    per_task_per_day: 3
    per_task_lifetime: 10
    cooldown_seconds: 60
"""


class ExampleConfigExistsError(DispatchError):
    """There is already a dispatch config, so the example was not written.

    Refusing rather than merging or backing up. This file is the record of what may
    execute here; a command that rewrites it is a command that can quietly widen it, and
    the person running ``--write`` on a machine that is already configured has almost
    certainly mistaken which machine they are on.
    """

    reason = "config_exists"


def write_example_config(home: Optional[Path] = None) -> Path:
    """Write the example to this machine's dispatch config path, if nothing is there.

    Returns the path written. Raises ``ExampleConfigExistsError`` if any file already
    occupies it -- including an empty or unparseable one, because "there is something
    here I did not read" is exactly when overwriting is worst.
    """
    path = dispatch_config_path(home)
    if path.exists():
        raise ExampleConfigExistsError(
            f"{path} already exists, so nothing was written. Read it, and edit it by "
            "hand: this file is the record of what this machine will execute, and a "
            "command that rewrites it is a command that can widen it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return path
