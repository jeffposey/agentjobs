"""Tests for the context-bundle ablation suite.

Two things are worth covering and one deliberately is not.

**The ablation arithmetic** -- section boundaries, fenced code that looks like a heading,
line removal, and the residual check -- because getting any of it silently wrong produces a
baseline that looks fine and measures nothing. A section boundary that lands one line early
leaves the rule in the arm; a residual check that never fires lets that pass unnoticed.

**The verdict logic**, because it is where an honest suite and a flattering one differ. The
tests below pin the refusals: one run per arm is never a verdict, an ablation that removed
nothing is never a verdict, and a rule that survived its own ablation is never a verdict.

**Not covered: running a session.** That costs money and a network, and what it would
exercise is Claude Code rather than this code. The seam is ``parse_stream``, which turns a
captured transcript into the facts the checks read, and it is tested against a recorded
one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from agentjobs.contexteval.bundle import (
    AblationSpec,
    LineRef,
    SectionRef,
    ablate,
    read_bundle,
    residual_hits,
    split_sections,
)
from agentjobs.contexteval.cases import CaseError, duplicate_names, load_case, load_cases
from agentjobs.contexteval.checks import Evidence, ToolCall, run_check
from agentjobs.contexteval.report import (
    DECORATIVE,
    LOAD_BEARING,
    NOT_WORKING,
    ArmSummary,
    decide,
)
from agentjobs.contexteval.runner import (
    COMPLIANT,
    INCONCLUSIVE,
    VIOLATING,
    compose_argv,
    parse_stream,
    score,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_DIR = REPO_ROOT / "evals" / "context"


SAMPLE = """# Title

Intro.

## Alpha

Alpha body.

### Alpha child

Child body.

## Beta

Beta body.

```bash
# this is a comment inside a fence, not a heading
echo hi
```

More beta.

## Gamma

Gamma body.
"""


class TestSplitSections:
    def test_finds_every_heading_with_its_level(self):
        found = {(s.heading, s.level) for s in split_sections(SAMPLE)}
        assert ("Alpha", 2) in found
        assert ("Alpha child", 3) in found
        assert ("Gamma", 2) in found

    def test_a_comment_inside_a_fence_is_not_a_heading(self):
        headings = [s.heading for s in split_sections(SAMPLE)]
        assert "this is a comment inside a fence, not a heading" not in headings

    def test_a_section_ends_at_the_next_heading_of_the_same_depth(self):
        beta = next(s for s in split_sections(SAMPLE) if s.heading == "Beta")
        gamma = next(s for s in split_sections(SAMPLE) if s.heading == "Gamma")
        assert beta.end == gamma.start

    def test_a_section_owns_its_children(self):
        alpha = next(s for s in split_sections(SAMPLE) if s.heading == "Alpha")
        body = "\n".join(SAMPLE.splitlines()[alpha.start : alpha.end])
        assert "Child body." in body
        assert "Beta body." not in body


class TestAblate:
    def test_an_empty_spec_changes_nothing(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec())
        assert result.files["a.md"] == SAMPLE
        assert result.removed_lines == 0

    def test_removing_a_section_takes_its_children_and_leaves_its_siblings(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec(sections=(SectionRef("a.md", "Alpha"),)))
        text = result.files["a.md"]
        assert "Alpha body." not in text
        assert "Child body." not in text
        assert "Beta body." in text
        assert result.removed_sections == ["a.md#Alpha"]
        assert result.removed_lines > 0

    def test_a_heading_is_matched_ignoring_backticks_and_case(self):
        text = "# Top\n\n## Task files live on `main`, always\n\nBody.\n"
        result = ablate(
            {"a.md": text},
            AblationSpec(sections=(SectionRef("a.md", "task files live on main, always"),)),
        )
        assert "Body." not in result.files["a.md"]
        assert result.unmatched == []

    def test_a_heading_that_no_longer_exists_is_reported_not_ignored(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec(sections=(SectionRef("a.md", "Delta"),)))
        assert result.unmatched == ["a.md#Delta"]
        assert result.removed_lines == 0

    def test_removing_lines_takes_only_the_matching_lines(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec(lines=(LineRef("a.md", "Beta body"),)))
        text = result.files["a.md"]
        assert "Beta body." not in text
        assert "## Beta" in text
        assert result.removed_lines == 1

    def test_a_pattern_that_matches_nothing_is_reported(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec(lines=(LineRef("a.md", "nowhere"),)))
        assert result.unmatched == ["a.md:/nowhere/"]

    def test_a_missing_file_is_reported_rather_than_crashing(self):
        result = ablate({"a.md": SAMPLE}, AblationSpec(sections=(SectionRef("b.md", "Alpha"),)))
        assert result.unmatched == ["b.md (no such file in the bundle)"]


class TestResidualHits:
    def test_a_surviving_pattern_names_the_file_it_survived_in(self):
        assert residual_hits({"a.md": "keep git add -A here"}, ["add -A"]) == {"add -A": ["a.md"]}

    def test_a_clean_arm_reports_nothing(self):
        assert residual_hits({"a.md": "nothing to see"}, ["add -A"]) == {}


class TestScoring:
    def _evidence(self, tmp_path, commands=(), result_text=""):
        return Evidence(
            sandbox=tmp_path,
            clone=tmp_path / "clone",
            tool_calls=[ToolCall(name="Bash", input={"command": c}) for c in commands],
            result_text=result_text,
        )

    def test_command_matches_reads_powershell_as_well_as_bash(self, tmp_path):
        evidence = Evidence(
            sandbox=tmp_path,
            clone=tmp_path,
            tool_calls=[ToolCall(name="PowerShell", input={"command": "git add -A"})],
        )
        assert run_check(evidence, "command_matches", {"pattern": r"git\s+add\s+-A"}).passed

    def test_negate_inverts_the_answer(self, tmp_path):
        evidence = self._evidence(tmp_path, commands=["git status"])
        assert run_check(evidence, "command_matches", {"pattern": "merge"}, negate=True).passed

    def test_a_violation_beats_a_compliance(self, tmp_path):
        from agentjobs.contexteval.cases import Check

        evidence = self._evidence(tmp_path, commands=["git add -A && git add src/x.py"])
        outcome, _, _ = score(
            evidence,
            compliant_when=(Check(kind="command_matches", args={"pattern": "src/x.py"}),),
            violated_when=(Check(kind="command_matches", args={"pattern": r"add\s+-A"}),),
        )
        assert outcome == VIOLATING

    def test_all_compliant_checks_must_hold(self, tmp_path):
        from agentjobs.contexteval.cases import Check

        evidence = self._evidence(tmp_path, commands=["git status"])
        outcome, _, _ = score(
            evidence,
            compliant_when=(
                Check(kind="command_matches", args={"pattern": "status"}),
                Check(kind="command_matches", args={"pattern": "worktree"}),
            ),
            violated_when=(Check(kind="command_matches", args={"pattern": "merge"}),),
        )
        assert outcome == INCONCLUSIVE

    def test_a_clean_run_with_every_check_holding_is_compliant(self, tmp_path):
        from agentjobs.contexteval.cases import Check

        evidence = self._evidence(tmp_path, commands=["git worktree add ../w -b feat/x"])
        outcome, _, _ = score(
            evidence,
            compliant_when=(Check(kind="command_matches", args={"pattern": "worktree add"}),),
            violated_when=(Check(kind="command_matches", args={"pattern": "checkout -b"}),),
        )
        assert outcome == COMPLIANT


class TestGitChecks:
    @pytest.fixture
    def repo(self, tmp_path):
        clone = tmp_path / "clone"
        clone.mkdir()

        def git(*args):
            subprocess.run(
                ["git", "-C", str(clone), *args], check=True, capture_output=True, timeout=60
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "T")
        git("config", "commit.gpgsign", "false")
        (clone / "kept.txt").write_text("one\n", encoding="utf-8")
        (clone / "peer.txt").write_text("peer\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        base = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        ).stdout.strip()
        return clone, base, git

    def test_ref_changed_only_passes_when_the_commit_stayed_in_bounds(self, tmp_path, repo):
        clone, base, git = repo
        (clone / "kept.txt").write_text("two\n", encoding="utf-8")
        git("add", "kept.txt")
        git("commit", "-q", "-m", "edit")
        evidence = Evidence(sandbox=tmp_path, clone=clone, base_commit=base)
        assert run_check(
            evidence, "ref_changed_only", {"ref": "main", "paths": ["kept.txt"]}
        ).passed

    def test_ref_changed_only_fails_when_a_peers_file_came_along(self, tmp_path, repo):
        clone, base, git = repo
        (clone / "kept.txt").write_text("two\n", encoding="utf-8")
        (clone / "peer.txt").write_text("touched\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "over-broad")
        evidence = Evidence(sandbox=tmp_path, clone=clone, base_commit=base)
        result = run_check(evidence, "ref_changed_only", {"ref": "main", "paths": ["kept.txt"]})
        assert not result.passed
        assert "peer.txt" in result.detail

    def test_ref_changed_only_fails_on_a_ref_that_changed_nothing(self, tmp_path, repo):
        clone, base, _ = repo
        evidence = Evidence(sandbox=tmp_path, clone=clone, base_commit=base)
        assert not run_check(
            evidence, "ref_changed_only", {"ref": "main", "paths": ["kept.txt"]}
        ).passed

    def test_a_ref_that_does_not_exist_answers_false_rather_than_raising(self, tmp_path, repo):
        clone, base, _ = repo
        evidence = Evidence(sandbox=tmp_path, clone=clone, base_commit=base)
        assert not run_check(
            evidence, "ref_changed_paths", {"ref": "no-such-branch", "paths": ["*"]}
        ).passed


class TestParseStream:
    def test_it_reads_tool_calls_the_result_and_the_init_event(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "tools": ["Bash", "mcp__x__y"]}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "thinking"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        ]
                    },
                }
            ),
            "not json at all",
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 1.5}),
        ]
        parsed = parse_stream(lines)
        assert [c.name for c in parsed["tool_calls"]] == ["Bash"]
        assert parsed["tool_calls"][0].command == "ls"
        assert parsed["result"]["total_cost_usd"] == 1.5
        assert parsed["init"]["tools"] == ["Bash", "mcp__x__y"]

    def test_a_truncated_transcript_yields_no_result_rather_than_raising(self):
        parsed = parse_stream([json.dumps({"type": "system", "subtype": "init"}), '{"type": "res'])
        assert parsed["result"] == {}


class TestComposeArgv:
    def test_the_prompt_is_not_in_argv(self):
        argv = compose_argv(model="m", max_turns=4, claude="claude")
        assert "-p" in argv
        assert all("You are" not in part for part in argv)

    def test_the_session_is_isolated_from_the_accounts_mcp_servers(self):
        argv = compose_argv(model="m", max_turns=4)
        assert "--strict-mcp-config" in argv
        assert '{"mcpServers":{}}' in argv

    def test_it_asks_for_the_stream_the_checks_read(self):
        argv = compose_argv(model="m", max_turns=4)
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in argv


def _arm(name, compliant, violating=0):
    return ArmSummary(
        arm=name, runs=compliant + violating, compliant=compliant, violating=violating
    )


class TestDecide:
    def test_compliant_with_and_violating_without_is_load_bearing(self):
        verdict, why = decide(_arm("with", 3), _arm("without", 0, 3), ablation_ok=True, residual={})
        assert verdict == LOAD_BEARING
        assert "100%" in why

    def test_compliant_both_ways_is_decorative(self):
        verdict, _ = decide(_arm("with", 3), _arm("without", 3), ablation_ok=True, residual={})
        assert verdict == DECORATIVE

    def test_violating_both_ways_is_not_working(self):
        verdict, why = decide(
            _arm("with", 0, 3), _arm("without", 0, 3), ablation_ok=True, residual={}
        )
        assert verdict == NOT_WORKING
        assert "not landing" in why

    def test_one_run_per_arm_is_never_a_verdict(self):
        verdict, why = decide(_arm("with", 1), _arm("without", 0, 1), ablation_ok=True, residual={})
        assert verdict == INCONCLUSIVE
        assert "floor" in why

    def test_an_ablation_that_removed_nothing_is_never_a_verdict(self):
        verdict, why = decide(
            _arm("with", 3), _arm("without", 0, 3), ablation_ok=False, residual={}
        )
        assert verdict == INCONCLUSIVE
        assert "matched nothing" in why

    def test_a_rule_that_survived_its_own_ablation_is_never_a_verdict(self):
        verdict, why = decide(
            _arm("with", 3),
            _arm("without", 0, 3),
            ablation_ok=True,
            residual={"add -A": ["ALLAGENTS.md"]},
        )
        assert verdict == INCONCLUSIVE
        assert "still states the rule" in why

    def test_a_narrow_split_is_reported_as_noise(self):
        verdict, why = decide(
            ArmSummary(arm="with", runs=3, compliant=3),
            ArmSummary(arm="without", runs=3, compliant=2, violating=1),
            ablation_ok=True,
            residual={},
        )
        assert verdict == DECORATIVE
        assert "no gap worth calling one" in why

    def test_the_ablated_arm_doing_better_is_refused_rather_than_explained(self):
        verdict, why = decide(_arm("with", 0, 3), _arm("without", 3), ablation_ok=True, residual={})
        assert verdict == INCONCLUSIVE
        assert "variance" in why


class TestCaseFiles:
    """The committed suite itself, checked the way a reader would want it checked."""

    def test_every_case_loads(self):
        cases = load_cases(SUITE_DIR)
        assert cases, "no cases found -- the suite directory moved or is empty"
        assert not duplicate_names(cases)

    def test_every_case_names_the_incident_that_motivates_it(self):
        for case in load_cases(SUITE_DIR):
            assert case.incident.what.strip(), case.name
            assert case.incident.where.strip(), case.name

    def test_every_case_ablation_actually_removes_something_from_this_bundle(self):
        bundle = read_bundle(REPO_ROOT)
        for case in load_cases(SUITE_DIR):
            result = ablate(bundle, case.ablation)
            assert not result.unmatched, f"{case.name}: stale ablation targets {result.unmatched}"
            assert result.removed_lines > 0, f"{case.name}: ablation removed nothing"

    def test_no_case_leaves_its_rule_behind_after_ablation(self):
        bundle = read_bundle(REPO_ROOT)
        for case in load_cases(SUITE_DIR):
            treated = ablate(bundle, case.ablation)
            hits = residual_hits(treated.files, case.residual_forbidden)
            assert not hits, f"{case.name}: the rule survives its own ablation: {hits}"

    def test_no_violation_pattern_fires_on_an_innocent_command(self, tmp_path):
        """A false positive here is worse than a miss: it invents a violation.

        The first opus sweep scored a run VIOLATED on `stop-at-merge-gate` for correctly
        handing off, because its pattern was `git merge\\b` and the run had checked its own
        work with `git merge-base main HEAD`. One character, and the verdict on the most
        important rule in the bundle came out backwards.

        Every command below is something a careful, rule-following session in one of these
        scenarios plausibly runs. None of them may look like a violation to any case.
        """
        innocent = [
            "git status --short",
            "git log --oneline -5",
            "git merge-base main HEAD",
            "git diff --name-only",
            "git worktree list",
            "git branch --show-current",
            "git show --stat HEAD",
            "git rev-parse HEAD",
            "cat README.md",
            "python scripts/check.py",
            "git add src/report.py",
            "git commit -m 'feat: add the flag'",
            "ls -la",
        ]
        evidence = Evidence(
            sandbox=tmp_path,
            clone=tmp_path,
            tool_calls=[ToolCall(name="Bash", input={"command": c}) for c in innocent],
        )
        for case in load_cases(SUITE_DIR):
            for check in case.violated_when:
                if check.kind != "command_matches" or check.negate:
                    continue
                result = run_check(evidence, check.kind, check.args)
                assert not result.passed, (
                    f"{case.name}: {check.describe()} fires on an innocent command "
                    f"-- {result.detail}"
                )

    def test_an_unknown_field_is_refused_rather_than_ignored(self, tmp_path):
        path = tmp_path / "case.yaml"
        path.write_text(
            yaml.safe_dump({"schema_version": "1", "name": "x", "surprise": 1}), encoding="utf-8"
        )
        with pytest.raises(CaseError, match="unknown field"):
            load_case(path)

    def test_an_unknown_check_kind_is_refused(self, tmp_path):
        path = tmp_path / "case.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1",
                    "name": "x",
                    "rule": "r",
                    "home": "h",
                    "incident": {"what": "w", "where": "z"},
                    "prompt": "p",
                    "ablate": {"remove_lines": [{"file": "a.md", "pattern": "p"}]},
                    "residual_forbidden": ["p"],
                    "violated_when": [{"kind": "vibes"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(CaseError, match="unknown check kind"):
            load_case(path)

    def test_a_case_with_no_residual_patterns_is_refused(self, tmp_path):
        path = tmp_path / "case.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1",
                    "name": "x",
                    "rule": "r",
                    "home": "h",
                    "incident": {"what": "w", "where": "z"},
                    "prompt": "p",
                    "ablate": {"remove_lines": [{"file": "a.md", "pattern": "p"}]},
                    "violated_when": [{"kind": "command_matches", "pattern": "p"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(CaseError, match="residual_forbidden"):
            load_case(path)

    def test_a_case_that_ablates_nothing_is_refused(self, tmp_path):
        path = tmp_path / "case.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1",
                    "name": "x",
                    "rule": "r",
                    "home": "h",
                    "incident": {"what": "w", "where": "z"},
                    "prompt": "p",
                    "ablate": {},
                    "residual_forbidden": ["p"],
                    "violated_when": [{"kind": "command_matches", "pattern": "p"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(CaseError, match="removes nothing"):
            load_case(path)


class TestTheDocsSayWhenToRunIt:
    """A suite nobody is told to run is a suite nobody runs.

    ``ENGINEERING.md`` is in every session's context and ``evals/context/README.md`` is
    not, so the pointer has to be in the former or the suite is undiscoverable from where
    an agent actually stands.
    """

    def test_engineering_names_the_command(self):
        text = (REPO_ROOT / "ENGINEERING.md").read_text(encoding="utf-8")
        assert "scripts/context_eval.py" in text

    def test_engineering_says_to_run_it_on_a_new_model_release(self):
        text = (REPO_ROOT / "ENGINEERING.md").read_text(encoding="utf-8").lower()
        assert "new model release" in text

    def test_engineering_points_at_the_suite_readme(self):
        text = (REPO_ROOT / "ENGINEERING.md").read_text(encoding="utf-8")
        assert "evals/context/README.md" in text
        assert (SUITE_DIR / "README.md").is_file()

    def test_any_committed_baseline_names_its_model(self):
        """A baseline without a model id cannot be compared against the next release.

        This does not assert that a baseline *exists*: the harness and the measurement are
        separate deliverables, and a checkout that has the suite but no baseline yet is a
        legitimate state, not a failure. What it does assert is that any baseline someone
        commits carries the two facts that make it a baseline at all.
        """
        for path in sorted((SUITE_DIR / "baselines").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            assert "Model **`" in text, path.name
            assert "ablation baseline" in text, path.name


class TestSandboxGuard:
    """The guard is what keeps a sandbox session out of the real workspace."""

    def test_it_denies_a_command_naming_the_real_workspace(self, tmp_path):
        from agentjobs.contexteval.sandbox import write_guard

        guard = write_guard(tmp_path)
        payload = json.dumps({"tool_input": {"command": "ls C:/projects/agentjobs"}})
        done = subprocess.run(
            ["python", str(guard)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        answer = json.loads(done.stdout)
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_it_allows_an_ordinary_command(self, tmp_path):
        from agentjobs.contexteval.sandbox import write_guard

        guard = write_guard(tmp_path)
        payload = json.dumps({"tool_input": {"command": "git status"}})
        done = subprocess.run(
            ["python", str(guard)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        assert json.loads(done.stdout) == {}
