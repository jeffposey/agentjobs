"""Write the tracked ``ROADMAP.md`` from the task store, or say it is stale.

The same contract ``scripts/export_openapi.py`` already satisfies for ``openapi.json``:
one render, written on demand and compared against **the working tree** under
``--check``, so a stale artefact fails the gate instead of rotting silently. The
comparison is against the tree rather than ``HEAD`` for the reason task-189 gives --
the question is whether the files a commit is about to carry match what the application
produces, and half of them are usually not committed yet.

One thing differs from the OpenAPI export and is worth stating plainly, because it is
the only reason this script is not a straight copy of that one. ``openapi.json`` is
derived from code in the checkout, so every machine renders the same document. This is
derived from a database **outside** the checkout, which has two consequences:

**A machine with no database for this project cannot verify the file, and says so
rather than failing.** Somebody who clones the public repository has no store, and a
gate that went red for them would be asserting something the machine cannot know. The
check prints what it could not reach and exits 0. On a machine that *is* serving the
project, the check is real.

**The file goes stale for reasons outside the branch.** Any task created, closed,
reordered, retitled or re-summarised anywhere makes every open branch's copy stale. That
is the cost of a live roadmap and it is deliberately cheap to pay: the failure names the
one command that fixes it, and the projection carries nothing that churns for a reason a
reader would not care about (no timestamps, no ball state, no run ids).
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Optional

from agentjobs.projects import Project, ProjectError, ProjectRegistry
from agentjobs.roadmap import RoadmapLeakError, database_for, roadmap_for

REGENERATE = "poetry run python scripts/export_roadmap.py ROADMAP.md"


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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Path to the checked-in roadmap")
    parser.add_argument("--project", default=None, help="Project id (default: this repository's)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report instead of writing when the checked-in roadmap is stale",
    )
    args = parser.parse_args(argv)

    project = resolve_project(args.project, Path.cwd())
    if project is None:
        print(
            "No AgentJobs project is registered for this checkout, so the roadmap "
            "cannot be verified against a store. Nothing was checked."
        )
        return 0 if args.check else 1

    database = database_for(project)
    if not database.is_file():
        print(
            f"Project {project.id!r} has no database at {database}, so the roadmap "
            "cannot be verified against a store. Nothing was checked."
        )
        return 0 if args.check else 1

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
