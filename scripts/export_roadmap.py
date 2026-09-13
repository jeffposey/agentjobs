"""Write the tracked ``docs/backlog.md`` from the task store, or say whether it is stale.

One render, written on demand. ``--check`` compares it against **the working tree** and
reports whether the committed copy has fallen behind the store, but **the gate does not
run it**. It did until task-413: any task created, closed or reordered anywhere made
every open branch's copy stale and turned every gate red, so each merge carried a
regeneration commit and two approved branches conflicted on the file. The owner never
agreed to that cost, and the listing is now regenerated when the ``roadmap`` playbook
runs rather than on every task change. ``--check`` stays as a question a person can ask.

One thing differs from the OpenAPI export and is worth stating plainly, because it is
the only reason this script is not a straight copy of that one. ``openapi.json`` is
derived from code in the checkout, so every machine renders the same document. This is
derived from a database **outside** the checkout, which has two consequences:

**A machine with no database for this project cannot verify the file, and says so
rather than failing.** Somebody who clones the public repository has no store, and a
gate that went red for them would be asserting something the machine cannot know. The
check prints what it could not reach and exits 0. On a machine that *is* serving the
project, the check is real.

**The file goes stale for reasons outside the branch**, and that is now accepted rather
than gated. The projection still carries nothing that churns for a reason a reader would
not care about (no timestamps, no ball state, no run ids), so a regeneration's diff is
only the backlog moving.

``--audit`` is the other half, and it is a different kind of check on a different kind of
file. ``ROADMAP.md`` is written by a person: the phases, the workstreams, and what each
one is for -- the things no projection of the records can produce, because no record
carries them. Nothing regenerates it, so nothing can compare it against a render. What
can be compared is its *claims*: every task it rosters should still be open work. Stale
ids fail; open tasks it has not yet placed are counted and let through, for the reason
``NarrativeAudit`` gives.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Optional

from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.roadmap import (
    RoadmapLeakError,
    audit_narrative_for,
    database_for,
    roadmap_for,
)

REGENERATE = "poetry run python scripts/export_roadmap.py docs/backlog.md"


def repository_root(start: Path) -> Path:
    """The clone this checkout belongs to, which is where the project is registered.

    A worktree lives outside the clone -- ``worktrees/agentjobs-392`` beside
    ``agentjobs`` -- so resolving a project from the working directory finds nothing and
    raises. ``--git-common-dir`` points at the *shared* git directory in every worktree
    of a clone, so its parent is the registered root from anywhere in the family.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=start,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return start
    if completed.returncode != 0 or not completed.stdout.strip():
        return start
    return (start / completed.stdout.strip()).resolve().parent


def resolve_project(project_id: Optional[str], start: Path) -> Optional[Project]:
    """The project whose store this roadmap is of, or None if nothing claims it.

    None is a real answer rather than an error: a clone on a machine that has never run
    AgentJobs has no registry, and that is the case ``--check`` reports instead of
    failing.
    """
    registry = ProjectRegistry()
    try:
        if project_id:
            return registry.get(project_id)
        return registry.resolve_default(repository_root(start))
    except (ProjectError, KeyError):
        return None


def audit(project: Project, narrative: Path) -> int:
    """Check what a hand-written roadmap page claims against the project's store.

    Two outcomes are possible and only one of them is red. A rostered task that is closed
    or was never filed is a lie the page tells a reader, and it fails. An open task the
    page has not placed in a workstream is lateness rather than error -- the generated
    listing beside it carries that task in full -- so it is counted and let through.
    """
    if not narrative.is_file():
        print(f"{narrative} does not exist, so there is no roadmap page to audit.")
        return 1

    result = audit_narrative_for(project, narrative.read_text(encoding="utf-8"))

    if result.stale:
        print(f"{narrative} rosters work that is no longer open:")
        for task_id in result.stale:
            print(f"  {task_id} is closed, archived, a draft, or was never filed")
        print("Remove it from its workstream, or move it to what replaced it.")
        return 1

    placed = f"{narrative} rosters {len(result.placed)} open tasks"
    if result.unplaced:
        preview = ", ".join(result.unplaced[:8])
        more = "" if len(result.unplaced) <= 8 else f", and {len(result.unplaced) - 8} more"
        print(f"{placed}; {len(result.unplaced)} are not placed in a workstream yet.")
        print(f"  {preview}{more}")
        print(f"They are listed in full by `{REGENERATE}`. Run the roadmap playbook to place them.")
    else:
        print(f"{placed}, which is every open task in the {project.id} store.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Path to the checked-in listing")
    parser.add_argument("--project", default=None, help="Project id (default: this repository's)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report instead of writing when the checked-in listing is stale",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=None,
        help="Audit a hand-written roadmap page instead: fail on a task it rosters "
        "that is no longer open, and report the open tasks it has not placed",
    )
    args = parser.parse_args(argv)

    unverifiable = args.check or args.audit is not None

    project = resolve_project(args.project, Path.cwd())
    if project is None:
        print(
            "No AgentJobs project is registered for this checkout, so the roadmap "
            "cannot be verified against a store. Nothing was checked."
        )
        return 0 if unverifiable else 1

    database = database_for(project)
    if not database.is_file():
        print(
            f"Project {project.id!r} has no database at {database}, so the roadmap "
            "cannot be verified against a store. Nothing was checked."
        )
        return 0 if unverifiable else 1

    if args.audit is not None:
        return audit(project, args.audit)

    try:
        expected = roadmap_for(project)
    except RoadmapLeakError as exc:
        print("The roadmap was not written: a record names a person or this machine.")
        for leak in exc.leaks:
            print(f"  {leak.render()}")
        print("Fix the task record -- the roadmap is a projection of it, not a place to edit.")
        return 1

    if args.check:
        actual = args.output.read_text(encoding="utf-8") if args.output.is_file() else None
        if actual != expected:
            print(f"{args.output} is stale; run `{REGENERATE}` and commit the result.")
            return 1
        print(f"{args.output} matches the {project.id} task store.")
        return 0

    args.output.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output} from the {project.id} task store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
