"""Measure a task corpus against ALLAGENTS' Resumption Contract.

The corpus-wide half of task-306. The per-record half is ``agentjobs.record_check``,
which fires at the moment a record is written and is silent otherwise; this is the view
for whoever is *auditing* rather than authoring, and it deliberately runs only when
someone asks for it. It is not in ``scripts/check.py`` and should not be: nothing here
can fail, and a gate stage that cannot fail is a stage people stop reading.

It answers three questions the 2026-08-21 context audit had to answer by writing a
throwaway script (``audits/2026-08-21/01-context-architecture.md`` section 6), which is
why the next audit had no baseline to compare against:

* How long are summaries, by era, against the contract's "one or two sentences"?
* How often does a `question` entry ever reach a record, per 100 log entries?
* How many existing records would each write-path warning fire on?

    python scripts/corpus_stats.py                    # this repository's own corpus
    python scripts/corpus_stats.py --tasks-dir <path> # somebody else's

The last question is the one that keeps the design honest. The write-path warnings are
silent on old records by construction, so without this the size of the backlog they are
*not* reporting would be invisible -- and "how bad is it really" is exactly what an
auditor is here to ask.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentjobs.models_v2 import Task  # noqa: E402
from agentjobs.record_check import (  # noqa: E402
    SUMMARY_WORD_CEILING,
    WARNING_KINDS,
    check_record,
    summary_words,
)
from agentjobs.taskfiles import TaskFileCorpus  # noqa: E402

#: The eras the audit split the corpus into, by leading task number. Kept because a
#: single median over the whole corpus hides the drift entirely -- the finding was that
#: the number is *moving*, not that it is high.
#:
#: ``001-030`` is a bucket of its own rather than folded into the first era, because
#: those ids are not from that era at all: finding F-15 of the same audit established
#: that the corpus carries two id series, and 001-024 were created in August 2026 after
#: task-105 changed allocation. Folding them in raises the oldest era's median by seven
#: words and quietly destroys the comparison this table exists to make.
ERAS: Sequence[tuple[str, int, int]] = (
    ("001-030", 0, 30),
    ("031-105", 31, 105),
    ("106-185", 106, 185),
    ("186-", 186, 10**9),
)


def default_tasks_dir() -> Path:
    """This repository's own corpus, resolved from the project config."""
    import yaml

    config_path = ROOT / ".agentjobs" / "config.yaml"
    configured = "tasks"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            configured = str(raw.get("tasks_directory") or configured)
    return (ROOT / configured).resolve()


def leading_number(task_id: str) -> Optional[int]:
    """The numeric part of ``task-NNN``, or None for an id that has none.

    Ids are not monotonic in this corpus -- finding F-15 of the same audit -- so this
    is a bucketing convenience and never an ordering.
    """
    parts = task_id.split("-")
    for part in parts[1:]:
        digits = "".join(char for char in part if char.isdigit())
        if digits:
            return int(digits)
    return None


def era_of(task_id: str) -> str:
    """Which era bucket a task id falls in."""
    number = leading_number(task_id)
    if number is None:
        return "unnumbered"
    for name, low, high in ERAS:
        if low <= number <= high:
            return name
    return "unnumbered"


def summary_report(tasks: Sequence[Task]) -> List[str]:
    """Summary length overall and by era, against the contract's ceiling."""
    lines = ["spec.summary length, in words (the contract asks for one or two sentences)"]
    buckets: Dict[str, List[int]] = {}
    for task in tasks:
        buckets.setdefault(era_of(task.id), []).append(summary_words(task.spec.summary))
    every = [count for counts in buckets.values() for count in counts]
    order = [name for name, _, _ in ERAS] + ["unnumbered"]
    for name in order:
        counts = sorted(buckets.get(name, []))
        if not counts:
            continue
        over = sum(1 for count in counts if count > SUMMARY_WORD_CEILING)
        lines.append(
            f"  {name:<12} n={len(counts):<4} median {int(statistics.median(counts)):<4} "
            f"max {max(counts):<4} over {SUMMARY_WORD_CEILING}: {over} "
            f"({_percent(over, len(counts))})"
        )
    if every:
        over = sum(1 for count in every if count > SUMMARY_WORD_CEILING)
        lines.append(
            f"  {'all':<12} n={len(every):<4} median {int(statistics.median(every)):<4} "
            f"max {max(every):<4} over {SUMMARY_WORD_CEILING}: {over} "
            f"({_percent(over, len(every))})"
        )
    return lines


def log_report(tasks: Sequence[Task]) -> List[str]:
    """Every log entry type, and the rate per 100 entries.

    ``question`` and ``answer`` are the two the audit was actually asking about -- an
    uncertainty raised only in chat is not queryable and does not survive the session --
    but printing them alone would give no sense of scale, and the finding was a
    comparison: 15 questions against 262 progress entries.
    """
    counts: Counter[str] = Counter()
    for task in tasks:
        for entry in task.log:
            counts[entry.type.value] += 1
    total = sum(counts.values())
    lines = [f"Log entries: {total} across {len(tasks)} records"]
    if not total:
        return lines
    for name, count in counts.most_common():
        marker = " <-" if name in ("question", "answer") else ""
        lines.append(f"  {name:<18} {count:<6} {count * 100 / total:>6.1f} per 100{marker}")
    questions = counts.get("question", 0)
    answers = counts.get("answer", 0)
    lines.append("")
    lines.append(
        f"  questions + answers: {questions + answers} "
        f"({(questions + answers) * 100 / total:.1f} per 100 entries) "
        f"in {_tasks_with_questions(tasks)} of {len(tasks)} records"
    )
    return lines


def warning_report(tasks: Sequence[Task]) -> List[str]:
    """How many existing records each write-path warning would fire on.

    Called with no verb, which is the corpus form of the check and the one the module's
    docstring tells write paths never to use. Here it is the point: this is the backlog
    the write-path warnings are deliberately silent about.
    """
    counts: Counter[str] = Counter()
    for task in tasks:
        for warning in check_record(task):
            counts[warning.kind] += 1
    lines = [
        "Records the write-path warnings would fire on if they swept the corpus",
        "  (they do not -- they fire only on the write that could have caused one)",
    ]
    for kind in WARNING_KINDS:
        count = counts.get(kind, 0)
        lines.append(f"  {kind:<22} {count:<6} ({_percent(count, len(tasks))})")
    return lines


def _tasks_with_questions(tasks: Sequence[Task]) -> int:
    """Records carrying at least one question or answer entry."""
    return sum(
        1 for task in tasks if any(entry.type.value in ("question", "answer") for entry in task.log)
    )


def _percent(part: int, whole: int) -> str:
    """A percentage, or a dash when the denominator is zero."""
    return f"{part * 100 / whole:.0f}%" if whole else "-"


def report(tasks_dir: Path, today: str) -> str:
    """The whole report, for one corpus."""
    loaded = TaskFileCorpus(tasks_dir, create=False).load_all()
    tasks = sorted(loaded.tasks, key=lambda task: task.id)
    blocks = [
        "AgentJobs record-quality statistics",
        f"Corpus: {tasks_dir}",
        f"Records: {len(tasks)} readable, {len(loaded.errors)} unreadable. Measured {today}.",
        "",
        "\n".join(summary_report(tasks)),
        "",
        "\n".join(log_report(tasks)),
        "",
        "\n".join(warning_report(tasks)),
    ]
    return "\n".join(blocks)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure a task corpus against the Resumption Contract."
    )
    parser.add_argument(
        "--tasks-dir",
        type=Path,
        default=None,
        help="The corpus to measure. Defaults to this repository's own.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="The date to stamp the report with. Defaults to today.",
    )
    args = parser.parse_args(argv)
    tasks_dir = (args.tasks_dir or default_tasks_dir()).resolve()
    if not tasks_dir.is_dir():
        print(f"Not a directory: {tasks_dir}", file=sys.stderr)
        return 2
    print()
    print(report(tasks_dir, args.date or date.today().isoformat()))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
