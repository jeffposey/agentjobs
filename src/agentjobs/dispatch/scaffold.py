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
# argv is recorded verbatim in the task's dispatch entry. Secrets go in `env:`, which is
# never logged.
runners:
  claude-standard:
    argv: ["claude", "--bg", "-p", "{prompt}"]
    mode: session
    actor: claude          # the identity it writes as, from the project's actors:

  claude-deep:
    argv: ["claude", "--bg", "--effort", "high", "-p", "{prompt}"]
    mode: session
    actor: claude

  claude-quick:
    argv: ["claude", "--bg", "--model", "haiku", "-p", "{prompt}"]
    mode: session
    actor: claude

  codex:
    argv: ["codex", "exec", "{prompt}"]
    mode: batch
    actor: codex

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
        note: Second option; enable once codex is installed and signed in.

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
#     posture: supervised      # read_only | supervised | autonomous
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
