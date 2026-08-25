"""Turning runs into the verdict task-304 asked for, and refusing to overstate it.

Per arm, a scenario's runs reduce to a **compliance rate**: how many of them did the thing
the rule exists to produce. Comparing the two arms gives one of four answers, and the
fourth is the honest one that most report generators leave out.

============ ================================================ =========================
verdict      what the two arms did                            what task-305 may do
============ ================================================ =========================
load_bearing compliant with the rule, not compliant without   keep the section
decorative   compliant both ways                              cut it, with evidence
not_working  violating both ways                              rewrite it or delete it
inconclusive anything that a small sample cannot separate     run more, decide nothing
============ ================================================ =========================

**A one-run-per-arm sweep can only ever be inconclusive**, and this module says so rather
than reporting a 1-0 split as a discovery. The threshold is deliberately crude and stated
in the output: a difference counts when the two arms' compliance rates differ by at least
``MIN_GAP`` and the suite ran at least ``MIN_RUNS`` per arm. There is no significance test
here because there is no sample size that would justify one at this cost, and a p-value on
three runs would dress up a coin flip.

The report also carries what would otherwise be invisible and would invalidate a verdict:
whether the ablation actually removed anything, whether a forbidden pattern survived it,
and whether the private global file states the rule too. Any of those makes the verdict
``inconclusive`` regardless of what the runs did, because the experiment did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from agentjobs.contexteval.runner import COMPLIANT, INCONCLUSIVE, VIOLATING, RunRecord

MIN_RUNS = 2
"""Runs per arm below which no verdict but ``inconclusive`` is available."""

MIN_GAP = 0.5
"""How far apart two arms' compliance rates must be before the difference is called one.

Half. With three runs an arm scores 0, 1/3, 2/3 or 1, so this asks for at least a two-run
swing out of three -- a 3-0 or a 2-0. A 2-1 split is noise at this sample size and is
reported as such.
"""

LOAD_BEARING = "load_bearing"
DECORATIVE = "decorative"
NOT_WORKING = "not_working"


@dataclass
class ArmSummary:
    """One arm of one scenario, reduced."""

    arm: str
    runs: int = 0
    compliant: int = 0
    violating: int = 0
    inconclusive: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    denials: int = 0

    @property
    def scored(self) -> int:
        return self.compliant + self.violating + self.inconclusive

    @property
    def compliance(self) -> float:
        return self.compliant / self.scored if self.scored else 0.0


@dataclass
class CaseVerdict:
    """A scenario's answer, and everything needed to disbelieve it."""

    name: str
    rule: str
    home: str
    incident_what: str
    incident_where: str
    verdict: str
    why: str
    with_arm: ArmSummary
    without_arm: ArmSummary
    removed_sections: List[str] = field(default_factory=list)
    removed_lines: int = 0
    unmatched: List[str] = field(default_factory=list)
    residual: Dict[str, List[str]] = field(default_factory=dict)
    global_agents: Dict[str, bool] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "rule": self.rule,
            "home": self.home,
            "incident": {"what": self.incident_what, "where": self.incident_where},
            "verdict": self.verdict,
            "why": self.why,
            "arms": {
                arm.arm: {
                    "runs": arm.runs,
                    "compliant": arm.compliant,
                    "violating": arm.violating,
                    "inconclusive": arm.inconclusive,
                    "errors": arm.errors,
                    "compliance": round(arm.compliance, 3),
                    "cost_usd": round(arm.cost_usd, 4),
                    "duration_s": round(arm.duration_s, 1),
                    "guard_denials": arm.denials,
                }
                for arm in (self.with_arm, self.without_arm)
            },
            "ablation": {
                "removed_sections": self.removed_sections,
                "removed_lines": self.removed_lines,
                "unmatched": self.unmatched,
                "residual_after_ablation": self.residual,
                "also_stated_in_global_agents": self.global_agents,
            },
        }


Verdict = CaseVerdict


def summarise(arm: str, records: Sequence[RunRecord]) -> ArmSummary:
    summary = ArmSummary(arm=arm)
    for record in records:
        summary.runs += 1
        summary.cost_usd += record.cost_usd
        summary.duration_s += record.duration_s
        summary.denials += len(record.denials)
        if record.error:
            summary.errors += 1
        elif record.outcome == COMPLIANT:
            summary.compliant += 1
        elif record.outcome == VIOLATING:
            summary.violating += 1
        else:
            summary.inconclusive += 1
    return summary


def decide(
    with_arm: ArmSummary,
    without_arm: ArmSummary,
    *,
    ablation_ok: bool,
    residual: Mapping[str, Sequence[str]],
) -> tuple:
    """The verdict and the sentence explaining it."""
    if not ablation_ok:
        return INCONCLUSIVE, (
            "the ablation matched nothing it was told to remove, so the two arms were "
            "not different -- fix the case before reading anything into the runs"
        )
    if residual:
        stated = ", ".join(
            f"{pattern!r} in {', '.join(files)}" for pattern, files in residual.items()
        )
        return INCONCLUSIVE, (
            f"the ablated arm still states the rule ({stated}), so this measured the removal "
            "of one statement rather than of the rule"
        )
    if with_arm.scored < MIN_RUNS or without_arm.scored < MIN_RUNS:
        return INCONCLUSIVE, (
            f"only {with_arm.scored} scored run(s) with the rule and {without_arm.scored} "
            f"without; {MIN_RUNS} per arm is the floor for any other verdict"
        )

    here, gone = with_arm.compliance, without_arm.compliance
    gap = here - gone
    rates = f"{here:.0%} compliant with the rule, {gone:.0%} without"
    if gap >= MIN_GAP:
        return LOAD_BEARING, f"{rates} -- removing it changed what the agent did"
    if here >= 1.0 - 1e-9 and gone >= 1.0 - 1e-9:
        return DECORATIVE, f"{rates} -- the agent did the right thing without being told"
    if here <= 0.0 and gone <= 0.0:
        return NOT_WORKING, f"{rates} -- the rule is loaded every session and is not landing"
    if abs(gap) < MIN_GAP and here >= 1.0 - 1e-9:
        return DECORATIVE, f"{rates} -- no gap worth calling one"
    if gap <= -MIN_GAP:
        return INCONCLUSIVE, (
            f"{rates} -- the arm *without* the rule did better, which is not a result this "
            "design can explain and is most likely variance"
        )
    return INCONCLUSIVE, f"{rates} -- the arms are within noise of each other at this sample size"


def build_report(
    cases: Sequence[Any],
    records: Sequence[RunRecord],
    ablations: Mapping[str, Mapping[str, Any]],
    *,
    meta: Mapping[str, Any],
) -> Dict[str, Any]:
    """Assemble the JSON report. ``ablations`` is keyed by case name."""
    verdicts: List[CaseVerdict] = []
    for case in cases:
        mine = [r for r in records if r.case == case.name]
        with_arm = summarise("with", [r for r in mine if r.arm == "with"])
        without_arm = summarise("without", [r for r in mine if r.arm == "without"])
        info = ablations.get(case.name, {})
        residual = dict(info.get("residual") or {})
        removed_sections = list(info.get("removed_sections") or [])
        unmatched = list(info.get("unmatched") or [])
        removed_lines = int(info.get("removed_lines") or 0)
        ablation_ok = bool(removed_lines) and not unmatched
        verdict, why = decide(with_arm, without_arm, ablation_ok=ablation_ok, residual=residual)
        verdicts.append(
            CaseVerdict(
                name=case.name,
                rule=case.rule,
                home=case.home,
                incident_what=case.incident.what,
                incident_where=case.incident.where,
                verdict=verdict,
                why=why,
                with_arm=with_arm,
                without_arm=without_arm,
                removed_sections=removed_sections,
                removed_lines=removed_lines,
                unmatched=unmatched,
                residual=residual,
                global_agents=dict(info.get("global_agents") or {}),
            )
        )

    total_cost = sum(r.cost_usd for r in records)
    return {
        "meta": dict(meta),
        "totals": {
            "cases": len(verdicts),
            "sessions": len(records),
            "cost_usd": round(total_cost, 4),
            "wall_clock_s": round(float(meta.get("wall_clock_s") or 0.0), 1),
            "session_seconds": round(sum(r.duration_s for r in records), 1),
        },
        "cases": [v.to_json() for v in verdicts],
        "runs": [r.to_json() for r in records],
    }


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> List[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"]
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows:
        out.append("| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")
    return out


def render_markdown(report: Mapping[str, Any]) -> str:
    """The committed baseline, as prose a person will actually read."""
    meta = report.get("meta", {})
    totals = report.get("totals", {})
    lines: List[str] = []
    lines.append(f"# Context-bundle ablation baseline: {meta.get('date', 'unknown date')}")
    lines.append("")
    lines.append(
        f"Model **`{meta.get('model', 'unknown')}`** | Claude Code `{meta.get('claude_version', '?')}` "
        f"| bundle at commit `{meta.get('bundle_commit', '?')}` | runs per arm: "
        f"{meta.get('runs_per_arm', '?')}"
    )
    lines.append("")
    lines.append(
        f"{totals.get('cases', 0)} scenario(s), {totals.get('sessions', 0)} session(s), "
        f"${totals.get('cost_usd', 0):.2f}, {totals.get('wall_clock_s', 0) / 60:.1f} min wall clock "
        f"({totals.get('session_seconds', 0) / 60:.1f} min of session time)."
    )
    lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    rows = []
    for case in report.get("cases", []):
        arms = case["arms"]
        rows.append(
            [
                f"`{case['name']}`",
                case["home"],
                f"{arms['with']['compliance']:.0%}",
                f"{arms['without']['compliance']:.0%}",
                f"**{case['verdict']}**",
            ]
        )
    lines.extend(
        _table(rows, ["scenario", "rule's home in the bundle", "with", "without", "verdict"])
    )
    lines.append("")
    lines.append(
        "`with` and `without` are the share of that arm's runs that did what the rule asks. "
        "A verdict of `load_bearing` means removing the section changed what the agent did; "
        "`decorative` means it did not; `not_working` means the rule is loaded every session "
        "and the agent violated it anyway; `inconclusive` means this sample cannot tell."
    )
    lines.append("")
    for case in report.get("cases", []):
        arms = case["arms"]
        lines.append(f"### `{case['name']}`: {case['verdict']}")
        lines.append("")
        lines.append(f"**Rule.** {case['rule']}")
        lines.append("")
        lines.append(f"**Home.** {case['home']}")
        lines.append("")
        lines.append(f"**Incident.** {case['incident']['what']} ({case['incident']['where']})")
        lines.append("")
        lines.append(f"**Result.** {case['why']}")
        lines.append("")
        for name in ("with", "without"):
            arm = arms[name]
            lines.append(
                f"- `{name}`: {arm['compliant']} compliant, {arm['violating']} violating, "
                f"{arm['inconclusive']} inconclusive, {arm['errors']} errored "
                f"of {arm['runs']} run(s); ${arm['cost_usd']:.2f}; "
                f"{arm['guard_denials']} sandbox-guard denial(s)"
            )
        ablation = case["ablation"]
        lines.append(
            f"- ablated: {ablation['removed_lines']} line(s), "
            f"section(s) {ablation['removed_sections'] or 'none'}"
            + (f"; UNMATCHED TARGETS {ablation['unmatched']}" if ablation["unmatched"] else "")
            + (
                f"; RULE SURVIVED ABLATION in {ablation['residual_after_ablation']}"
                if ablation["residual_after_ablation"]
                else ""
            )
        )
        also = [p for p, hit in (ablation.get("also_stated_in_global_agents") or {}).items() if hit]
        if also:
            lines.append(
                f"- **also stated in the private global file** (present in both arms, not "
                f"ablatable): {also}. Read this verdict as conditional on that."
            )
        lines.append("")
    return "\n".join(lines) + "\n"
