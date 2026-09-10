# AGENTS.md

This file serves as the automatically loaded entry point for AI agents working on the
AgentJobs repository.

## Core Documentation
- [ENGINEERING.md](ENGINEERING.md) - Universal engineering standards, tech stack details, and git workflow. **Read this first.**
- [ALLAGENTS.md](ALLAGENTS.md) - Shared guidance for AI agent workflows, reporting standards, and behavioral expectations.

## Required Context Loading

Claude imports the two documents above through `CLAUDE.md`. Codex automatically loads
this file, not a sibling `CODEX.md`; before any task work, branch creation, worktree
creation, or repository edit, Codex **must read `ENGINEERING.md` and `ALLAGENTS.md` in
full, in that order**. Links in this file are required context, not optional reference
material.

## Branch and Worktree Lifecycle

These rules are repeated here because they must be in the automatically loaded context:

- In a shared clone, take a task-named worktree before creating a branch or writing
  code. Never use `git checkout` to start work in the shared clone.
- Record the active branch in the task record. Task records are rows in a database
  outside the checkout, so there is nothing to commit for one.
- After explicit approval and a successful merge, delete the merged local branch and
  remove its worktree. A worktree for a closed task is litter.
- Before ending a task, inspect `git worktree list`. Clean up only your own merged,
  clean worktrees; reconcile any unmerged commits or uncommitted changes before
  removal.
