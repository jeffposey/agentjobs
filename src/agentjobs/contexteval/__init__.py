"""Measuring which rules in the always-loaded context bundle change what an agent does.

Every file a session in this repository loads before its first thought is a per-session
tax paid forever, and until this package existed the only way to decide whether a
paragraph earned its place was to read it and form an opinion. Audit 1 (task-242) did
exactly that and produced a keep/compress/move/delete table -- a good table, and
unfalsifiable as written.

This package makes the same claim falsifiable. For a rule, it builds a scratch repository
whose ``CLAUDE.md`` chain is a copy of this one's, runs a real headless Claude Code
session against a scenario the rule exists to prevent, and scores the session on what it
*did* -- which ref a commit landed on, whether a worktree was taken, which paths were
staged. Then it does the same thing again with that rule cut out of the copy, and
compares.

Three verdicts come out, and the spec of task-304 says why each one is worth having:

``load_bearing``
    Compliant with the rule loaded, violating without it. Measured, not argued. The
    section stays, and nobody has to re-litigate it.
``decorative``
    Compliant both ways. The model already knows, or the scenario tells it. The section
    can be cut *for this model* with evidence rather than nerve.
``not_working``
    Violating both ways. The most valuable of the three: the rule is present, is loaded
    every session, and is not producing the behaviour it was written for. No amount of
    reading the file surfaces this.

A fourth, ``inconclusive``, is reported honestly rather than rounded into one of the
others -- too few runs, a split that could be noise, or a scenario whose session did not
get far enough to be scored.

**The answer is expected to move when the model does.** That is the point of keeping it
as a suite rather than doing it once: a rule that is load-bearing for one generation may
be redundant with the next model's defaults, and a newer model may need a rule an older
one absorbed from the situation. ``evals/context/README.md`` says when to re-run it.
"""

from agentjobs.contexteval.bundle import (
    BUNDLE_FILES,
    AblationSpec,
    SectionRef,
    LineRef,
    ablate,
    read_bundle,
    split_sections,
)
from agentjobs.contexteval.cases import Case, CaseError, load_cases, load_case
from agentjobs.contexteval.checks import CheckResult, Evidence, run_check
from agentjobs.contexteval.report import (
    ArmSummary,
    CaseVerdict,
    Verdict,
    build_report,
    render_markdown,
)

__all__ = [
    "BUNDLE_FILES",
    "AblationSpec",
    "ArmSummary",
    "Case",
    "CaseError",
    "CaseVerdict",
    "CheckResult",
    "Evidence",
    "LineRef",
    "SectionRef",
    "Verdict",
    "ablate",
    "build_report",
    "load_case",
    "load_cases",
    "read_bundle",
    "render_markdown",
    "run_check",
    "split_sections",
]
