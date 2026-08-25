#!/usr/bin/env python
"""Run the context-bundle ablation suite.

    poetry run python scripts/context_eval.py --list
    poetry run python scripts/context_eval.py --tag quick
    poetry run python scripts/context_eval.py --model claude-opus-5 --runs 3

Each scenario runs twice per repetition: once against a copy of this repository's
always-loaded bundle, and once against the same copy with one rule cut out. The difference
in what the agent *did* is the measurement. ``evals/context/README.md`` says when to run it
and how to read the output; the design and what it rejected are on task-304.

Nothing here writes to the live backlog, the shared clone or ``main``. Every session runs in
a throwaway git repository under the OS temp directory, behind a hook that refuses any tool
call naming the real workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentjobs.contexteval.bundle import (  # noqa: E402
    AblationSpec,
    ablate,
    global_agents_hits,
    read_bundle,
    residual_hits,
)
from agentjobs.contexteval.cases import Case, load_cases  # noqa: E402
from agentjobs.contexteval import report as report_mod  # noqa: E402
from agentjobs.contexteval import runner as runner_mod  # noqa: E402
from agentjobs.contexteval import sandbox as sandbox_mod  # noqa: E402

SUITE_DIR = REPO_ROOT / "evals" / "context"
DEFAULT_MODEL = "claude-opus-5"
GLOBAL_AGENTS = Path.home() / ".claude" / "CLAUDE.md"


def _stamp(text: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}", flush=True)


def _claude_version(claude: str) -> str:
    try:
        done = subprocess.run(
            [claude, "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (done.stdout or "").strip().split()[0] if done.stdout else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def _bundle_commit() -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (done.stdout or "").strip() or "?"
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return "?"


def _resolve_global_agents() -> Path:
    """The private global file, following the one ``@`` import it carries.

    ``~/.claude/CLAUDE.md`` is normally a single ``@`` line pointing at the real text, and
    grepping the pointer for a rule would answer "no" every time.
    """
    if not GLOBAL_AGENTS.is_file():
        return GLOBAL_AGENTS
    text = GLOBAL_AGENTS.read_text(encoding="utf-8", errors="replace").strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("@"):
            candidate = Path(line[1:].strip())
            if candidate.is_file():
                return candidate
    return GLOBAL_AGENTS


def prepare(case: Case, bundle: Dict[str, str]) -> Dict[str, Any]:
    """Render both arms for a case and check the ablation actually did something."""
    control = ablate(bundle, AblationSpec())
    treated = ablate(bundle, case.ablation)
    return {
        "with": control.files,
        "without": treated.files,
        "removed_sections": treated.removed_sections,
        "removed_lines": treated.removed_lines,
        "unmatched": treated.unmatched,
        "residual": residual_hits(treated.files, case.residual_forbidden),
        "global_agents": global_agents_hits(case.residual_forbidden, _resolve_global_agents()),
    }


def _run_unit(
    case: Case,
    arm: str,
    index: int,
    files: Dict[str, str],
    model: str,
    out_dir: Path,
    claude: str,
    keep: bool,
) -> runner_mod.RunRecord:
    # One unit's failure costs that unit, never the sweep. Thirty-six sessions is half an
    # hour and real money, and losing the lot to a git hiccup in the thirtieth sandbox is
    # not a trade worth taking -- an errored run already reports as an error in the arm
    # summary, and `decide` refuses a verdict when too few runs scored.
    try:
        box = sandbox_mod.build(case, files)
    except Exception as exc:  # noqa: BLE001 - the reason goes on the record
        _stamp(f"  {case.name} [{arm}] run {index}: SANDBOX FAILED: {exc}")
        return runner_mod.RunRecord(
            case=case.name,
            arm=arm,
            index=index,
            outcome=runner_mod.INCONCLUSIVE,
            model=model,
            error=f"could not build the sandbox: {exc}"[:500],
        )
    try:
        record = runner_mod.run_once(
            case, box, arm=arm, index=index, model=model, out_dir=out_dir, claude=claude
        )
    except Exception as exc:  # noqa: BLE001 - the reason goes on the record
        record = runner_mod.RunRecord(
            case=case.name,
            arm=arm,
            index=index,
            outcome=runner_mod.INCONCLUSIVE,
            model=model,
            error=f"the run raised: {exc}"[:500],
        )
    finally:
        if keep:
            _stamp(f"kept sandbox for {case.name}/{arm}/{index}: {box.root}")
        else:
            box.remove()
    marker = {"compliant": "ok", "violating": "VIOLATED", "inconclusive": "?"}.get(
        record.outcome, record.outcome
    )
    if record.error:
        marker = f"ERROR: {record.error[:80]}"
    _stamp(
        f"  {case.name} [{arm}] run {index}: {marker} "
        f"({record.duration_s:.0f}s, ${record.cost_usd:.3f}, {record.num_turns} turns)"
    )
    return record


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--case", help="glob over case names")
    parser.add_argument(
        "--tag", action="append", default=[], help="only cases carrying this tag (repeatable)"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"model under test (default: {DEFAULT_MODEL})"
    )
    parser.add_argument("--runs", type=int, help="override each case's runs-per-arm")
    parser.add_argument("--jobs", type=int, default=3, help="concurrent sessions (default: 3)")
    parser.add_argument("--out", help="results directory (default: evals/context/results/<stamp>)")
    parser.add_argument("--claude", help="path to the Claude Code executable")
    parser.add_argument("--keep", action="store_true", help="do not delete the sandboxes")
    parser.add_argument("--list", action="store_true", help="list the selected cases and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="render and validate both arms, run nothing"
    )
    args = parser.parse_args(argv)

    cases = load_cases(SUITE_DIR, name_glob=args.case, tags=args.tag)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    if args.list:
        for case in cases:
            print(
                f"{case.name:<28} runs={case.runs} tags={','.join(case.tags) or '-'}  {case.home}"
            )
            print(f"{'':<28} {case.rule}")
        return 0

    bundle = read_bundle(REPO_ROOT)
    missing = [
        name for name in ("CLAUDE.md", "ENGINEERING.md", "ALLAGENTS.md") if name not in bundle
    ]
    if missing:
        print(f"bundle files missing from {REPO_ROOT}: {missing}", file=sys.stderr)
        return 2

    prepared = {case.name: prepare(case, bundle) for case in cases}

    problems = False
    for case in cases:
        info = prepared[case.name]
        if info["unmatched"]:
            print(
                f"{case.name}: ablation targets matched nothing: {info['unmatched']}",
                file=sys.stderr,
            )
            problems = True
        if not info["removed_lines"]:
            print(f"{case.name}: ablation removed no lines at all", file=sys.stderr)
            problems = True
        if info["residual"]:
            print(f"{case.name}: rule survives the ablation: {info['residual']}", file=sys.stderr)
            problems = True
        also = [p for p, hit in info["global_agents"].items() if hit]
        if also:
            print(f"{case.name}: NOTE -- also stated in the private global file: {also}")

    if args.dry_run:
        for case in cases:
            info = prepared[case.name]
            print(
                f"{case.name}: removed {info['removed_lines']} line(s), "
                f"sections {info['removed_sections'] or 'none'}"
            )
        return 1 if problems else 0
    if problems:
        print("refusing to run: fix the case(s) above first", file=sys.stderr)
        return 2

    claude = runner_mod.resolve_claude(args.claude)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else SUITE_DIR / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    units = []
    for case in cases:
        repeats = args.runs if args.runs else case.runs
        for index in range(1, repeats + 1):
            for arm in ("with", "without"):
                units.append((case, arm, index, prepared[case.name][arm]))

    _stamp(
        f"{len(cases)} case(s), {len(units)} session(s) against {args.model} "
        f"at up to {args.jobs} at a time -> {out_dir}"
    )

    started = time.monotonic()
    records: List[runner_mod.RunRecord] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [
            pool.submit(_run_unit, case, arm, index, files, args.model, out_dir, claude, args.keep)
            for case, arm, index, files in units
        ]
        for future in futures:
            records.append(future.result())
    wall = time.monotonic() - started

    meta = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "claude_version": _claude_version(claude),
        "bundle_commit": _bundle_commit(),
        "runs_per_arm": args.runs or "per case",
        "jobs": args.jobs,
        "wall_clock_s": wall,
        "host_global_file_loaded": str(_resolve_global_agents()),
        "disallowed_tools": list(runner_mod.DISALLOWED_TOOLS),
    }
    report = report_mod.build_report(cases, records, prepared, meta=meta)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = report_mod.render_markdown(report)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")

    print()
    print(markdown)
    _stamp(f"wrote {out_dir / 'report.md'} and report.json")

    errored = sum(1 for record in records if record.error)
    if errored:
        print(f"{errored} session(s) failed to produce a result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
