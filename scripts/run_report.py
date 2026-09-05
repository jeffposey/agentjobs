"""Report where dispatched agent time goes, from the run ledger on this machine.

The table this prints is the one task-233 assembled by hand on 2026-08-21 -- 52 runs,
21.1 hours, 2.13 runs per task -- and assembling it by hand is the reason it had never
been assembled before. Everything here comes from ``~/.agentjobs/runs/*/``:

* ``meta.yaml`` for the outer shape of a run: task, outcome, start, finish.
* ``phases.jsonl`` for what happened inside one (``agentjobs.dispatch.phases``).

``transcript.log`` is deliberately not read. It is a raw TTY capture -- ANSI escapes and
screen redraws -- so a line appears in it as many times as the terminal repainted it,
and every count derived from it is an artefact of that. Phase records exist so this
script never has to guess.

    python scripts/run_report.py                 # the whole ledger
    python scripts/run_report.py --since 7       # runs started in the last 7 days
    python scripts/run_report.py --task task-233 # one task's runs, listed
    python scripts/run_report.py --per-task      # every task, worst first
    python scripts/run_report.py --epics         # every epic: wall clock, busy, idle
    python scripts/run_report.py --epic task-269 # one epic, with a row per child span

``--epics`` is the instrument task-223 needed and this script did not have. Runs and
finishes were each measured on their own, so **the gap between one child closing and the
next starting was in no table anywhere** -- which is exactly the quantity a concurrent
epic walk removes. It groups a run under the parent of its task, reads the parent from
the project's own corpus rather than from the ledger (nothing in ``meta.yaml`` records
one, so a ledger-only version could not baseline the serial epics already recorded), and
reports both the **union** of the child intervals and their **sum**. It needs both: a sum
stops being a share of a timeline the moment anything overlaps, and a union alone would
hide the improvement entirely, because running two children at once reduces the union and
the wall clock together. ``work / wall`` is the parallelism actually achieved, and 1.00 is
serial however many slots were configured.

``--split`` is how a cycle-time change is claimed rather than asserted: it prints the
table twice, either side of a moment, so the moment can be a merge commit's timestamp.
Pair it with ``--driver`` -- a window that introduced a second runner is not comparable
to one that had only the first::

    python scripts/run_report.py --driver claude --split 2026-08-21T23:24:52+00:00

Since task-241 it also reads ``~/.agentjobs/finishes/*/``, which is where a **scripted
finish** writes itself down. A finish is not a run -- no agent, no session, no tokens --
and it exists precisely to remove the follow-on run this report was built to measure. It
is counted separately for that reason: folded into ``runs`` it would inflate the figure
it reduces, and left out altogether the saving would read as missing data.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentjobs.dispatch.phases import read_phases  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - the package depends on it
    print("PyYAML is required. Run `python scripts/bootstrap.py`.", file=sys.stderr)
    raise SystemExit(1)


DEFAULT_GAP_CEILING_SECONDS = 3600.0
"""A gap longer than this ends an epic's sitting rather than counting as idle (task-223).

An hour, chosen against what the two kinds of gap actually look like on this machine's
ledger. Turnaround between children in a live walk is a poll interval plus a dispatch --
seconds to a couple of minutes. The gaps between sittings are overnight, or a weekend, or
a review nobody got to: task-160's children span five days. There is nothing in between
on any epic recorded so far, so the boundary is not a judgement call being smuggled in as
a constant; ``--gap-ceiling`` moves it when that stops being true.
"""

HOME_ENV = "AGENTJOBS_HOME"
GATE_STARTED = "gate_started"
GATE_FINISHED = "gate_finished"
FINISHES_DIRNAME = "finishes"


def default_home() -> Path:
    return Path(os.environ.get(HOME_ENV) or (Path.home() / ".agentjobs"))


def as_moment(value: object) -> Optional[datetime]:
    """A timestamp out of a record, naive values read as UTC, junk read as absent.

    A ``datetime`` is accepted as well as a string. Dispatch writes these through
    ``yaml.safe_dump``, which quotes them, so they come back as strings -- but YAML
    parses an *unquoted* ISO timestamp into a ``datetime``, and a hand-written or
    hand-corrected ``meta.yaml`` is unquoted. Refusing that reads as "this run has no
    duration", which is the one answer a reporting tool must not give by accident.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Gate:
    """One invocation of ``scripts/check.py`` inside a run."""

    seconds: float
    passed: bool
    scope: str
    stages_run: int
    stages_total: int
    failed_stage: Optional[str]

    abandoned: bool = False
    """Started and never finished: killed, timed out, or the session moved on (task-339).

    Its ``seconds`` is a floor rather than a measurement -- the elapsed time to the next
    thing the run did -- and every aggregate that uses it says so. Counting these is not
    bookkeeping: task-336 launched eight gates and ``gate_finished`` recorded six, so the
    report said 31.1 minutes of gate against the 48.4 the run actually paid, and the two
    invisible gates were the two longest single blocks in the run.
    """


@dataclass(frozen=True)
class Run:
    """One dispatched run, as the ledger and its phase records describe it."""

    run_id: str
    task_id: str
    outcome: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    gates: Sequence[Gate]
    driver: str = "claude"
    """Which runner drove the session -- ``claude`` or ``codex``.

    Absent from ``meta.yaml`` is read as ``claude``, because only the Codex path writes
    the key and every run predating that path was a Claude one. The fallback is stated
    rather than hidden because the whole use of this field is to compare like with like:
    task-233's after-window is half Codex bring-up runs, most of them startup failures
    under two minutes, and folding those into a cycle-time figure measures the bring-up
    rather than the cycle time.
    """
    resumed_from: Optional[str] = None
    """The run whose session this one resumed, or None for a cold start (task-234).

    This is what makes the before/after pair measurable from the ledger rather than
    asserted. A woken run and a cold one are otherwise indistinguishable here -- same
    mode, same shape, same phase records -- and the whole claim of task-234 is that one
    of them is much shorter than the other.
    """
    session_env: Optional[str] = None
    """How this run's identity reached its worker, or None if nothing recorded it.

    ``None`` is the honest answer for every run dispatched before task-249, and it is
    the distinction this field exists to make: a run with no phase records because the
    delivery did not exist yet is a different fact from one that had the delivery and
    ran no gate. See ``dispatch.session_env.Delivery`` for the vocabulary.
    """
    project_id: str = ""
    """Which registered project this run belongs to, or "" for a run that did not say.

    Read only so that a task id can be resolved to a parent in the right corpus
    (task-223). Two projects may both have a ``task-042``.
    """
    daemon_started: Optional[bool] = None
    """Whether this run's own launch started the Claude Code daemon (task-249).

    Recorded from the launcher's banner. Before the ``--settings`` delivery existed this
    was the whole of it: a run whose launch started the daemon got its own environment,
    and a run that joined a daemon somebody else started inherited that one's -- 12 of
    61 launches on this machine printed the banner. Kept afterwards because it is the
    evidence that the delivery, not the accident, is what is now doing the work.
    """

    @property
    def resumed(self) -> bool:
        return self.resumed_from is not None

    @property
    def seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def gate_seconds(self) -> float:
        return sum(gate.seconds for gate in self.gates)

    @property
    def wasted_gate_seconds(self) -> float:
        """Time in gate runs that failed or were abandoned. Not progress you keep."""
        return sum(gate.seconds for gate in self.gates if not gate.passed)

    @property
    def abandoned_gates(self) -> int:
        return sum(1 for gate in self.gates if gate.abandoned)

    @property
    def gates_overlapped(self) -> bool:
        """True when this run's gates cannot have been sequential.

        Summed gate time longer than the run itself means the session had two or more
        ``check.py`` invocations alive at once -- ``run_4063f1c0`` started three inside
        two minutes and each took about six. The phase records are right and the run
        duration is right; it is the *sum* that stops being a share of a timeline. Say
        so rather than printing a percentage over a hundred and letting a reader decide
        which number to distrust.
        """
        return self.seconds is not None and self.gate_seconds > self.seconds


def _abandoned_seconds(
    records: Sequence[Dict[str, object]], start: int, end: int
) -> Optional[float]:
    """How long a gate that never finished was demonstrably alive for.

    The floor is the elapsed time from its ``gate_started`` to the next thing the run
    recorded -- normally the ``gate_started`` of whatever the session ran instead. It is
    a floor and not a measurement, because nothing wrote down the moment the gate died;
    what is known is that the run was inside it until the next record exists.

    Checked against task-336: its abandoned gate at 19:50:12 is followed by a
    ``gate_started`` at 20:00:24, giving 612s -- which is the Bash tool's 600-second cap
    plus start-up, and matches the ten-minute hole in that run's timeline exactly.
    """
    began = as_moment(records[start].get("ts"))
    if began is None:
        return None
    after = records[end] if end < len(records) else (records[-1] if records else None)
    ended = as_moment(after.get("ts")) if after is not None else None
    if ended is None:
        return None
    seconds = (ended - began).total_seconds()
    return seconds if seconds > 0 else None


def read_gates(directory: Path) -> List[Gate]:
    """Every gate run recorded in a run directory, finished or not.

    A ``gate_started`` with no matching finish is a gate the run was killed in the middle
    of, and until task-339 it contributed nothing at all. That was the safer of the two
    errors -- ``RunRecord.elapsed_seconds`` refuses to report an unknown duration as a
    number, and this should not reintroduce that one directory over -- but it was still
    an error, and a large one: task-336's two abandoned gates were seventeen of its
    forty-eight gate minutes, and the report said the run had launched six gates when it
    had launched eight. A tool built to answer "where does the time go" cannot drop the
    two longest blocks in the run.

    So they are counted, with the duration marked as the floor it is: ``abandoned=True``,
    and every aggregate that prints their seconds says which part of the total is a floor.
    A gate whose start has no timestamp to measure from is still dropped; there the honest
    answer really is that nothing is known.
    """
    records = read_phases(directory)
    starts = [index for index, record in enumerate(records) if record.get("kind") == GATE_STARTED]
    consumed: set[int] = set()
    found: List[Tuple[int, Gate]] = []
    for position, index in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(records)
        finished = next(
            (
                offset
                for offset in range(index + 1, end)
                if records[offset].get("kind") == GATE_FINISHED
            ),
            None,
        )
        if finished is not None:
            consumed.add(finished)
            gate = _finished_gate(records[finished])
            if gate is not None:
                found.append((index, gate))
            continue
        elapsed = _abandoned_seconds(records, index, end)
        if elapsed is None:
            continue
        started = records[index]
        # The stage it died in, which is the useful half of "it was killed": task-336's
        # two both died in e2e, the tenth of ten, having paid for the other nine.
        inside = [
            str(records[offset].get("stage"))
            for offset in range(index + 1, end)
            if records[offset].get("kind") == "gate_stage_started"
        ]
        found.append(
            (
                index,
                Gate(
                    seconds=elapsed,
                    passed=False,
                    scope=str(started.get("scope") or "unknown"),
                    stages_run=len(inside),
                    stages_total=int(started.get("stages_total") or 0),
                    failed_stage=inside[-1] if inside else None,
                    abandoned=True,
                ),
            )
        )
    # A finish with no start in front of it is an old ledger, not a broken one: this
    # script reads directories written by whatever version dispatched them. Counting it
    # on the strength of its own record is what the tool did before task-339, and losing
    # recorded history to a stricter reader would be a poor trade.
    for index, record in enumerate(records):
        if record.get("kind") != GATE_FINISHED or index in consumed:
            continue
        gate = _finished_gate(record)
        if gate is not None:
            found.append((index, gate))
    return [gate for _, gate in sorted(found, key=lambda pair: pair[0])]


def _finished_gate(record: Dict[str, object]) -> Optional[Gate]:
    """One ``gate_finished`` record as a ``Gate``, or None when it carries no duration."""
    seconds = record.get("seconds")
    if not isinstance(seconds, (int, float)):
        return None
    failed_stage = record.get("failed_stage")
    return Gate(
        seconds=float(seconds),
        passed=bool(record.get("passed")),
        scope=str(record.get("scope") or "unknown"),
        stages_run=int(record.get("stages_run") or 0),
        stages_total=int(record.get("stages_total") or 0),
        failed_stage=str(failed_stage) if failed_stage else None,
    )


def read_run(directory: Path) -> Optional[Run]:
    """One run directory, or None when it holds no readable metadata."""
    meta_path = directory / "meta.yaml"
    if not meta_path.is_file():
        return None
    try:
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    meta = loaded if isinstance(loaded, dict) else {}
    return Run(
        run_id=str(meta.get("run_id") or directory.name),
        task_id=str(meta.get("task_id") or "?"),
        outcome=str(meta.get("outcome") or meta.get("status") or "unknown"),
        started_at=as_moment(meta.get("started_at")),
        finished_at=as_moment(meta.get("finished_at")),
        gates=read_gates(directory),
        driver=str(meta.get("driver") or "claude"),
        resumed_from=(
            str(meta["resumed_from"]) if isinstance(meta.get("resumed_from"), str) else None
        ),
        session_env=(
            str(meta["session_env"]) if isinstance(meta.get("session_env"), str) else None
        ),
        project_id=str(meta.get("project_id") or ""),
        daemon_started=(
            bool(meta["daemon_started"]) if isinstance(meta.get("daemon_started"), bool) else None
        ),
    )


@dataclass(frozen=True)
class Finish:
    """One scripted post-approval finish (task-241), as its own record describes it.

    ``outcome`` is one of ``finished``, ``escalated`` or ``declined``. Only the first is
    a follow-on run that did not have to happen; an escalated finish asked for a session
    (or left the task waiting for a human's Dispatch click), and a declined one means the
    task was never a candidate and nothing at all was done.
    """

    finish_id: str
    task_id: str
    outcome: str
    reason: str
    stopped_at: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    merged: bool
    gates: Sequence[Gate]
    project_id: str = ""

    @property
    def seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def gate_seconds(self) -> float:
        return sum(gate.seconds for gate in self.gates)


def read_finish(directory: Path) -> Optional[Finish]:
    """One finish directory, or None when it holds no readable metadata."""
    meta_path = directory / "meta.yaml"
    if not meta_path.is_file():
        return None
    try:
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    meta = loaded if isinstance(loaded, dict) else {}
    return Finish(
        finish_id=str(meta.get("finish_id") or directory.name),
        task_id=str(meta.get("task_id") or "?"),
        outcome=str(meta.get("outcome") or "unknown"),
        reason=str(meta.get("reason") or ""),
        stopped_at=str(meta.get("stopped_at") or ""),
        started_at=as_moment(meta.get("started_at")),
        finished_at=as_moment(meta.get("finished_at")),
        merged=bool(meta.get("merge_commit")),
        gates=read_gates(directory),
        project_id=str(meta.get("project_id") or ""),
    )


def load_finishes(home: Path) -> List[Finish]:
    """Every finish this machine has a directory for, oldest first."""
    root = home / FINISHES_DIRNAME
    if not root.is_dir():
        return []
    finishes = [
        finish
        for directory in sorted(root.iterdir())
        if directory.is_dir() and directory.name != "spawn"
        for finish in [read_finish(directory)]
        if finish is not None
    ]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(finishes, key=lambda finish: finish.started_at or epoch)


def load_runs(home: Path) -> List[Run]:
    """Every run this machine has a directory for, oldest first."""
    root = home / "runs"
    if not root.is_dir():
        return []
    runs = [
        run
        for directory in sorted(root.iterdir())
        if directory.is_dir() and directory.name != ".locks"
        for run in [read_run(directory)]
        if run is not None
    ]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(runs, key=lambda run: run.started_at or epoch)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. Samples are small, so interpolation is false precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f}m"


def hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def outcome_counts(runs: Iterable[Run]) -> str:
    counts: Dict[str, int] = {}
    for run in runs:
        counts[run.outcome] = counts.get(run.outcome, 0) + 1
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


def summary(runs: List[Run], finishes: Sequence[Finish] = ()) -> str:
    """The baseline table, reproduced from the ledger rather than from a transcript."""
    timed = [run for run in runs if run.seconds is not None]
    durations = [run.seconds or 0.0 for run in timed]
    tasks = {run.task_id for run in runs}
    total = sum(durations)
    gate_total = sum(run.gate_seconds for run in runs)
    wasted = sum(run.wasted_gate_seconds for run in runs)
    gate_runs = sum(len(run.gates) for run in runs)
    instrumented = [run for run in timed if run.gates]

    per_task_seconds: Dict[str, float] = {}
    for run in timed:
        per_task_seconds[run.task_id] = per_task_seconds.get(run.task_id, 0.0) + (
            run.seconds or 0.0
        )
    task_totals = sorted(per_task_seconds.values())

    lines = [
        f"  runs                  {len(runs)} ({outcome_counts(runs)})",
        f"  runs with durations   {len(timed)}",
        f"  total run time        {hours(total)}",
        f"  distinct tasks        {len(tasks)}",
        f"  runs per task         {len(runs) / len(tasks):.2f}",
        # Median as well as mean, because at these sample sizes one feature build --
        # task-081's epic before, task-241's own 123-minute run after -- moves the mean
        # by a factor and the median not at all. Quoting only the mean is how the
        # baseline first over-read itself.
        f"  time per task         mean {minutes(total / len(per_task_seconds))}, "
        f"median {minutes(percentile(task_totals, 0.50))}"
        if per_task_seconds
        else "  time per task         -",
        f"  run length            p50 {minutes(percentile(durations, 0.50))}, "
        f"p75 {minutes(percentile(durations, 0.75))}, "
        f"p90 {minutes(percentile(durations, 0.90))}, "
        f"max {minutes(max(durations, default=0.0))}",
    ]

    if gate_runs:
        share = gate_total / total * 100 if total else 0.0
        instrumented_total = sum(run.seconds or 0.0 for run in instrumented)
        instrumented_share = (
            sum(run.gate_seconds for run in instrumented) / instrumented_total * 100
            if instrumented_total
            else 0.0
        )
        abandoned = [gate for run in runs for gate in run.gates if gate.abandoned]
        # The two per-run figures are the pair task-339 is judged on, so they are printed
        # rather than left to be divided out of the two lines above. "Launched" is the
        # word deliberately: it counts the gates that were abandoned as well, which is
        # the whole of what changed here.
        per_run = f"{gate_runs / len(instrumented):.1f}" if instrumented else "-"
        per_run_minutes = minutes(gate_total / len(instrumented)) if instrumented else "-"
        lines += [
            "",
            f"  gate runs             {gate_runs} across {len(instrumented)} instrumented runs",
            f"  gates launched /run   {per_run}",
            f"  gate time             {hours(gate_total)} "
            f"({share:.0f}% of all run time, {instrumented_share:.0f}% of instrumented)",
            f"  gate time /run        {per_run_minutes}",
            f"  gate time thrown away {hours(wasted)} in gate runs that failed or were "
            "abandoned",
        ]
        if abandoned:
            lines += [
                f"  gates abandoned       {len(abandoned)} started and never finished, "
                f"{hours(sum(gate.seconds for gate in abandoned))} at least",
                "                        (elapsed to the next thing the run recorded, so "
                "a floor)",
            ]
        overlapped = [run for run in instrumented if run.gates_overlapped]
        if overlapped:
            lines += [
                f"  gates overlapped      in {len(overlapped)} run(s): "
                f"{', '.join(run.run_id for run in overlapped)}",
                "                        summed gate time exceeds the run, so the shares",
                "                        above are sums rather than shares of a timeline",
            ]
    lines += _instrumentation_lines(runs, gate_runs)
    lines += _resume_lines(timed)
    lines += _finish_lines(finishes)

    return "\n".join(lines)


DELIVERED = "delivered"
"""``session_env.Delivery.DELIVERED``, restated rather than imported.

This script reads run directories written by whatever version of AgentJobs dispatched
them, so the value is a wire constant here, not a live enum member.
"""


def _instrumentation_lines(runs: Sequence[Run], gate_runs: int) -> List[str]:
    """Why a run has no gate lines, told apart from the other reasons it might not.

    Three different facts used to print the same sentence, and only one of them is about
    the work (task-249):

    - **the instrumentation did not exist** when the run was dispatched -- no
      ``session_env`` key at all;
    - **the run's identity never reached its worker**, so the gate it ran had nowhere to
      write. That was the steady state under ``--bg``: the launcher's environment is
      discarded by the daemon that spawns the worker, and only 12 launches in 61 were
      the one that started the daemon;
    - **the identity arrived and no gate was ever run**, which is the only one of the
      three that says something about the session.

    Reading the first as the third is how the task-233 baseline came to be quoted from
    records that mostly did not exist.
    """
    if not runs:
        return []
    uninstrumented = [run for run in runs if run.session_env is None]
    undelivered = [
        run for run in runs if run.session_env is not None and run.session_env != DELIVERED
    ]
    silent = [
        run
        for run in runs
        if run.session_env == DELIVERED and not run.gates and run.outcome not in {"unknown"}
    ]

    lines: List[str] = []
    if not gate_runs:
        lines += ["", "  No gate lines can be computed for this window:"]
    elif uninstrumented or undelivered:
        lines += ["", "  Gate lines are missing for some runs in this window:"]
    else:
        return lines

    if uninstrumented:
        lines.append(
            f"  {len(uninstrumented)} run(s) predate the identity delivery (task-249) and "
            "carry no"
        )
        lines.append("      session_env key, so a missing gate line is not evidence either way.")
        inherited = [run for run in uninstrumented if run.daemon_started is False]
        if inherited:
            lines.append(
                f"      {len(inherited)} of those joined a daemon started by something else, "
                "so their"
            )
            lines.append("      environment was another run's and their gate records went astray.")
    if undelivered:
        reasons = ", ".join(sorted({str(run.session_env) for run in undelivered}))
        lines.append(
            f"  {len(undelivered)} run(s) had the delivery attempted and not completed "
            f"({reasons});"
        )
        lines.append("      their gates had nowhere to write.")
    if silent:
        lines.append(f"  {len(silent)} run(s) were instrumented and ran no gate at all.")
    return lines


def _resume_lines(timed: List[Run]) -> List[str]:
    """The wake's before/after, from the ledger (task-234).

    Only runs that were **not the first for their task** are compared. A task's first
    run has no session to resume and is long by nature -- it is the working run -- so
    averaging it in on the cold side would flatter the change by comparing a merge
    against a day's work. What is compared is like with like: second-and-later runs that
    resumed a conversation, against second-and-later runs that started cold.

    Silent when nothing has been resumed yet, so the report reads exactly as it did
    before this existed until there is something to say.
    """
    if not any(run.resumed for run in timed):
        return []
    first_of_task = {run.task_id: run.run_id for run in reversed(timed)}
    later = [run for run in timed if first_of_task.get(run.task_id) != run.run_id]
    cold = [run for run in later if not run.resumed]
    warm = [run for run in later if run.resumed]

    def mean(group: List[Run]) -> str:
        if not group:
            return "-"
        return minutes(sum(run.seconds or 0.0 for run in group) / len(group))

    return [
        "",
        f"  follow-on runs        {len(later)} (runs after a task's first)",
        f"    cold start          {len(cold)}, mean {mean(cold)}",
        f"    resumed session     {len(warm)}, mean {mean(warm)}",
    ]


def _finish_lines(finishes: Sequence[Finish]) -> List[str]:
    """The scripted finish's own block (task-241), and the third leg of the comparison.

    Kept separate from the cold/resumed pair above rather than added as a third row in
    it, because it is not the same kind of thing. Those two are follow-on *runs* and
    differ in how the agent started. A finish is the absence of a follow-on run, so its
    mean is not "how long the merge session took" but "how long there was no session
    for" -- and reading it off the same axis without saying so would invite exactly the
    comparison it should be making explicit.

    Silent when nothing has been finished, so the report reads as it did before.
    """
    if not finishes:
        return []
    done = [finish for finish in finishes if finish.outcome == "finished"]
    escalated = [finish for finish in finishes if finish.outcome == "escalated"]
    declined = [finish for finish in finishes if finish.outcome == "declined"]

    def mean(group: Sequence[Finish]) -> str:
        timed = [finish.seconds or 0.0 for finish in group if finish.seconds is not None]
        return minutes(sum(timed) / len(timed)) if timed else "-"

    lines = [
        "",
        f"  scripted finishes     {len(finishes)} (task-241; approval merges without an agent)",
        f"    finished            {len(done)}, mean {mean(done)}  <- follow-on runs that never happened",
    ]
    if escalated:
        stops = ", ".join(sorted({f"{finish.stopped_at or finish.reason}" for finish in escalated}))
        merged = sum(1 for finish in escalated if finish.merged)
        lines.append(
            f"    escalated           {len(escalated)}, mean {mean(escalated)}  "
            f"(stopped at: {stops}; {merged} had already merged)"
        )
    if declined:
        lines.append(
            f"    declined            {len(declined)}  (never a candidate; nothing was done)"
        )
    if done:
        lines.append(
            f"    tasks finished with no dispatched run: "
            f"{len({finish.task_id for finish in done})}"
        )
    return lines


# ----- epics: wall clock, and the idle this measurement exists for (task-223) -----


@dataclass(frozen=True)
class Span:
    """One occupied interval on an epic's timeline: a child run, or a child's finish."""

    task_id: str
    label: str
    started_at: datetime
    finished_at: datetime

    @property
    def seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class Epic:
    """One parent task's children, as intervals on a shared clock.

    **The quantity this exists for is ``idle``**, and until task-223 nothing in this
    ledger reported it. Runs and finishes were each measured on their own, so the gap
    between one child closing and the next starting -- the whole of what a serial walk
    spends and a concurrent one does not -- was in no table anywhere. A cycle-time claim
    is a before/after or it is an anecdote, and this is the instrument the before half
    has to come from.
    """

    parent_id: str
    spans: Sequence[Span]
    gap_ceiling_seconds: float = DEFAULT_GAP_CEILING_SECONDS

    @property
    def started_at(self) -> datetime:
        return min(span.started_at for span in self.spans)

    @property
    def finished_at(self) -> datetime:
        return max(span.finished_at for span in self.spans)

    @property
    def occupied(self) -> List[List[datetime]]:
        """The spans merged into non-overlapping intervals, in order.

        The union rather than the sum, because a sum exceeds the wall clock the moment
        anything runs in parallel -- which is the case this exists to measure, and
        ``gates_overlapped`` is where this file already learned not to report a sum as a
        share of a timeline.
        """
        merged: List[List[datetime]] = []
        for span in sorted(self.spans, key=lambda item: item.started_at):
            if merged and span.started_at <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], span.finished_at)
            else:
                merged.append([span.started_at, span.finished_at])
        return merged

    @property
    def sittings(self) -> List[List[List[datetime]]]:
        """Occupied intervals grouped into runs of work separated by short gaps.

        **The number a naive wall clock produces is nearly meaningless, and this is why.**
        An epic dispatched on Monday whose last child merges on Thursday has three days
        of wall clock, and about six hours of it is anything a scheduler could have
        affected; the rest is a person asleep, or a review nobody had got to, or the epic
        simply not being driven. Reporting that as 92% idle -- which the first cut of this
        did -- attributes a human's week to the walk.

        So a gap longer than ``gap_ceiling_seconds`` ends a sitting. Within a sitting the
        gaps are turnaround, which is what a concurrent walk removes and what this
        measurement is for. Between sittings is somebody's life, and it is reported
        separately rather than folded in or silently dropped.
        """
        groups: List[List[List[datetime]]] = []
        for interval in self.occupied:
            if groups and (interval[0] - groups[-1][-1][1]).total_seconds() <= (
                self.gap_ceiling_seconds
            ):
                groups[-1].append(interval)
            else:
                groups.append([interval])
        return groups

    @property
    def wall_seconds(self) -> float:
        """Time inside a sitting: what somebody watching this epic actually waited."""
        return sum((group[-1][1] - group[0][0]).total_seconds() for group in self.sittings)

    @property
    def busy_seconds(self) -> float:
        """Time inside a sitting with at least one child actually running."""
        return sum((end - start).total_seconds() for group in self.sittings for start, end in group)

    @property
    def work_seconds(self) -> float:
        """The **sum** of the child spans: how long the work took, ignoring overlap.

        The one figure here that is deliberately a sum rather than a union, and the one
        that makes a concurrency claim checkable. ``work / wall`` is the parallelism the
        epic actually achieved: 1.0 is serial however many slots were configured, and a
        four-slot walk of four independent children approaches 4. Reporting only the
        union would hide exactly the improvement task-223 exists to produce, because
        running two children at once *reduces* both wall and union together.
        """
        return sum(span.seconds for span in self.spans)

    @property
    def parallelism(self) -> float:
        return self.work_seconds / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def paused_seconds(self) -> float:
        """Time between sittings. Not the scheduler's, and not counted against it."""
        total = (self.finished_at - self.started_at).total_seconds()
        return max(0.0, total - self.wall_seconds)

    @property
    def idle_seconds(self) -> float:
        """Turnaround: eligible work waiting because the walk was doing one thing.

        This is the quantity task-223 removes, and the only one of the three that a
        scheduling change can move.
        """
        return max(0.0, self.wall_seconds - self.busy_seconds)

    @property
    def peak_concurrency(self) -> int:
        """The most spans alive at one instant. 1 means it ran serially.

        Spans are half-open: an end at the same instant as a start is a handover, not an
        overlap, so ends are applied first. Getting this backwards reports every clean
        serial handover as concurrency 2 -- which would have made the *baseline* look
        like the improvement.
        """
        events: List[tuple] = []
        for span in self.spans:
            events.append((span.started_at, 1))
            events.append((span.finished_at, -1))
        events.sort(key=lambda item: (item[0], item[1]))
        peak = current = 0
        for _moment, delta in events:
            current += delta
            peak = max(peak, current)
        return peak

    @property
    def children(self) -> int:
        return len({span.task_id for span in self.spans})


def load_parents(home: Path, project_ids: Iterable[str]) -> Dict[tuple, str]:
    """``(project_id, task_id) -> parent id`` for every task in the named projects.

    Read from the task corpora rather than from the ledger, deliberately. Nothing in
    ``meta.yaml`` records a run's parent, so a ledger-only implementation could group
    only the epics dispatched after it was added -- and the whole point of this is to
    baseline the serial epics that are **already** in the ledger. Resolving from the
    corpus works backwards over the entire history.

    The cost is that it reports the graph as it stands *now*: a child re-parented since
    it ran is grouped where it lives today. That is the right trade for a report and the
    wrong one for an audit; nothing here claims to be an audit.
    """
    import logging  # noqa: PLC0415 - only this mode reads a corpus

    from agentjobs.manager import TaskManager  # noqa: PLC0415 - optional for this mode
    from agentjobs.projects import ProjectRegistry
    from agentjobs.storage import TaskStorage

    # A task file this report cannot parse is not this report's business. Task loading
    # logs one error line per broken file, and a neighbouring project with twenty of them
    # would bury the table underneath them. Whether they are broken is a question
    # `agentjobs validate` answers properly.
    storage_log = logging.getLogger("agentjobs.storage")
    previous = storage_log.level
    storage_log.setLevel(logging.CRITICAL)
    parents: Dict[tuple, str] = {}
    try:
        registry = ProjectRegistry()
        for project_id in sorted({pid for pid in project_ids if pid}):
            try:
                project = registry.get(project_id)
                manager = TaskManager(TaskStorage(project.tasks_dir()))
                tasks = manager.storage.list_tasks()
            except Exception:  # noqa: BLE001 - an unreadable project groups nothing
                continue
            for task in tasks:
                if task.parent:
                    parents[(project_id, task.id)] = task.parent
    finally:
        storage_log.setLevel(previous)
    return parents


def build_epics(
    runs: Sequence[Run],
    finishes: Sequence[Finish],
    home: Path,
    gap_ceiling: float = DEFAULT_GAP_CEILING_SECONDS,
) -> List[Epic]:
    """Group every timed run and finish under the epic its task belongs to.

    A parent's *own* runs are excluded, and that exclusion is what makes the number mean
    anything: a supervising walk spans the entire epic by construction, so counting it as
    a span would make ``busy`` equal ``wall`` and ``idle`` zero for every epic ever run.
    What is measured here is the children.
    """
    project_ids = [run.project_id for run in runs] + [finish.project_id for finish in finishes]
    parents = load_parents(home, project_ids)
    if not parents:
        return []

    grouped: Dict[str, List[Span]] = {}
    for run in runs:
        parent = parents.get((run.project_id, run.task_id))
        if parent is None or run.started_at is None or run.finished_at is None:
            continue
        grouped.setdefault(parent, []).append(
            Span(run.task_id, run.run_id, run.started_at, run.finished_at)
        )
    for finish in finishes:
        parent = parents.get((finish.project_id, finish.task_id))
        if parent is None or finish.started_at is None or finish.finished_at is None:
            continue
        grouped.setdefault(parent, []).append(
            Span(finish.task_id, finish.finish_id, finish.started_at, finish.finished_at)
        )

    epics = [
        Epic(parent_id=parent, spans=spans, gap_ceiling_seconds=gap_ceiling)
        for parent, spans in grouped.items()
    ]
    return sorted(epics, key=lambda epic: epic.started_at)


def epic_report(epics: Sequence[Epic]) -> str:
    """A row per epic, plus the one line the whole of task-223 is aimed at."""
    if not epics:
        return (
            "  No epic could be reconstructed for this window. Either no run in it\n"
            "  belongs to a task with a parent, or the project's task corpus could not\n"
            "  be read from this machine."
        )
    width = max(max((len(epic.parent_id) for epic in epics), default=6), 6)
    lines = [
        f"  {'epic'.ljust(width)}  kids  sit  peak     wall     work     idle   idle%      x",
    ]
    for epic in epics:
        share = epic.idle_seconds / epic.wall_seconds * 100 if epic.wall_seconds else 0.0
        lines.append(
            f"  {epic.parent_id.ljust(width)}  {epic.children:>4}  "
            f"{len(epic.sittings):>3}  {epic.peak_concurrency:>4}  "
            f"{minutes(epic.wall_seconds):>7}  {minutes(epic.work_seconds):>7}  "
            f"{minutes(epic.idle_seconds):>7}  {share:5.1f}%  {epic.parallelism:5.2f}"
        )
    wall = sum(epic.wall_seconds for epic in epics)
    work = sum(epic.work_seconds for epic in epics)
    idle = sum(epic.idle_seconds for epic in epics)
    paused = sum(epic.paused_seconds for epic in epics)
    serial = [epic for epic in epics if epic.peak_concurrency <= 1]
    lines += [
        "",
        f"  epics                 {len(epics)}",
        f"  time in sittings      {hours(wall)}  (a gap over "
        f"{epics[0].gap_ceiling_seconds / 60:.0f}m ends one)",
        f"  child work            {hours(work)} summed, so parallelism "
        f"{work / wall if wall else 0:.2f}x  <- 1.00 is serial",
        f"  turnaround idle       {hours(idle)} ({idle / wall * 100 if wall else 0:.0f}% of it)"
        "  <- a slot free with eligible work waiting",
        f"  paused between        {hours(paused)}  (overnight, review, nobody driving --"
        " not the scheduler's)",
        f"  ran serially          {len(serial)} of {len(epics)} (peak concurrency 1)",
        "",
        "  `x` is the number this measures. Wall clock shrinks two ways under a",
        "  concurrent walk -- turnaround idle disappears, and children overlap -- and",
        "  only the second shows up here, because overlapping two children reduces the",
        "  union and the wall clock together.",
    ]
    return "\n".join(lines)


def per_task(runs: List[Run]) -> str:
    """Every task, most dispatched time first. Finding 2 of task-233 is this table."""
    by_task: Dict[str, List[Run]] = {}
    for run in runs:
        by_task.setdefault(run.task_id, []).append(run)
    rows = sorted(
        (
            (task, sum(run.seconds or 0.0 for run in group), len(group))
            for task, group in by_task.items()
        ),
        key=lambda row: row[1],
        reverse=True,
    )
    total = sum(row[1] for row in rows) or 1.0
    width = max((len(row[0]) for row in rows), default=4)
    lines = [f"  {'task'.ljust(width)}  runs   total   share"]
    for task, seconds, count in rows:
        share = seconds / total * 100
        lines.append(f"  {task.ljust(width)}  {count:>4}  {minutes(seconds):>6}  {share:5.1f}%")
    return "\n".join(lines)


def listing(runs: List[Run]) -> str:
    """A row per run: the raw material every aggregate above is built from."""
    width = max((len(run.run_id) for run in runs), default=6)
    lines = [f"  {'run'.ljust(width)}  task       outcome      elapsed  gates"]
    for run in runs:
        marker = " (resumed)" if run.resumed else ""
        elapsed = minutes(run.seconds) if run.seconds is not None else "-"
        if run.gates:
            failed = sum(1 for gate in run.gates if not gate.passed and not gate.abandoned)
            notes = [minutes(run.gate_seconds)]
            if failed:
                notes.append(f"{failed} failed")
            if run.abandoned_gates:
                notes.append(f"{run.abandoned_gates} abandoned")
            gates = f"{len(run.gates)} ({', '.join(notes)})"
        else:
            gates = "-"
        lines.append(
            f"  {run.run_id.ljust(width)}  {run.task_id:<10} {run.outcome:<11}  "
            f"{elapsed:>7}  {gates}{marker}"
        )
    return "\n".join(lines)


def split_report(
    home: Path,
    runs: Sequence[Run],
    finishes: Sequence[Finish],
    boundary: datetime,
) -> int:
    """The same table twice, either side of a moment. Task-233's acceptance t6 is this.

    A cycle-time claim is a before/after or it is an anecdote, and until this existed
    the only way to produce one was a throwaway script in a scratch directory -- which
    is precisely the hand-assembly this whole report was written to end. Pass the merge
    commit's timestamp and the tool states the claim.

    Two cautions, both learned from the run that first needed this:

    * **Split by driver too.** ``--driver claude --split ...`` is usually what you
      want. A window that introduced a second runner is not comparable to one that had
      only the first, and the newcomer's startup failures land as very short runs that
      move the percentiles a long way.
    * **Read the median, not the mean.** One feature build in a nine-task window moves
      the mean by a factor. Both are printed; the median is the one that survives a
      small sample.
    """
    before = [run for run in runs if run.started_at and run.started_at < boundary]
    after = [run for run in runs if run.started_at and run.started_at >= boundary]
    undated = len(runs) - len(before) - len(after)

    fin_before = [item for item in finishes if item.started_at and item.started_at < boundary]
    fin_after = [item for item in finishes if item.started_at and item.started_at >= boundary]

    print(f"\nDispatched runs in {home / 'runs'}, split at {boundary.isoformat()}\n")
    for label, group, group_finishes in (
        ("BEFORE", before, fin_before),
        ("AFTER", after, fin_after),
    ):
        print(f"{label}\n")
        if group:
            print(summary(group, group_finishes))
        elif group_finishes:
            print("\n".join(_finish_lines(group_finishes)).strip("\n"))
        else:
            print("  Nothing on this side of the boundary.")
        print()
    if undated:
        # Never silently. A run with no start time is one this split cannot place, and
        # dropping it without saying so is how a sample quietly stops being the corpus.
        print(f"  {undated} run(s) had no start time and are in neither side.\n")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report where dispatched agent time goes.")
    parser.add_argument(
        "--home", type=Path, default=None, help="ledger root (default ~/.agentjobs)"
    )
    parser.add_argument(
        "--since", type=float, metavar="DAYS", help="only runs started in the last N days"
    )
    parser.add_argument(
        "--split",
        metavar="WHEN",
        help="report twice, before and after this ISO date or timestamp (UTC)",
    )
    parser.add_argument(
        "--driver",
        metavar="NAME",
        help="only runs driven by this runner (claude, codex); absent in meta reads as claude",
    )
    parser.add_argument(
        "--task", metavar="TASK_ID", help="only this task's runs, listed individually"
    )
    parser.add_argument("--per-task", action="store_true", help="a row per task, most time first")
    parser.add_argument("--list", action="store_true", help="a row per run")
    parser.add_argument(
        "--epics",
        action="store_true",
        help="a row per epic: wall clock, busy time, and the idle between its children",
    )
    parser.add_argument(
        "--epic", metavar="TASK_ID", help="only this epic, with a row per child span"
    )
    parser.add_argument(
        "--gap-ceiling",
        type=float,
        default=DEFAULT_GAP_CEILING_SECONDS / 60.0,
        metavar="MINUTES",
        help="a gap longer than this ends an epic's sitting instead of counting as idle",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    home = args.home or default_home()
    runs = load_runs(home)
    finishes = load_finishes(home)
    if args.since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        runs = [run for run in runs if run.started_at and run.started_at >= cutoff]
        finishes = [
            finish for finish in finishes if finish.started_at and finish.started_at >= cutoff
        ]
    if args.driver:
        runs = [run for run in runs if run.driver == args.driver]
    if args.task:
        runs = [run for run in runs if run.task_id == args.task]
        finishes = [finish for finish in finishes if finish.task_id == args.task]

    if args.epics or args.epic:
        # Before the "no runs matched" guard below, because an epic is reconstructed from
        # the corpus and one selected by `--epic` may legitimately have no run of its own.
        epics = build_epics(runs, finishes, home, args.gap_ceiling * 60.0)
        if args.epic:
            epics = [epic for epic in epics if epic.parent_id == args.epic]
        print(f"\nEpics reconstructed from {home / 'runs'}\n")
        print(epic_report(epics))
        for epic in epics if args.epic else []:
            print(f"\nSpans of {epic.parent_id}, in start order\n")
            for span in sorted(epic.spans, key=lambda item: item.started_at):
                start = span.started_at.astimezone().strftime("%m-%d %H:%M")
                print(
                    f"  {span.task_id:<12} {span.label:<16} {start}  " f"{minutes(span.seconds):>7}"
                )
        print()
        return 0

    if args.split:
        boundary = as_moment(args.split)
        if boundary is None:
            print(f"Not an ISO date or timestamp: {args.split!r}", file=sys.stderr)
            return 2
        return split_report(home, runs, finishes, boundary)

    if not runs and not finishes:
        print(f"No runs in {home / 'runs'} matching that selection.")
        return 0

    if not runs:
        # A task finished with no dispatched run at all is the whole point of task-241,
        # and it is also the one case this report used to answer with "no runs" and stop.
        print(f"\nNo dispatched runs matched. Scripted finishes in {home / 'finishes'}\n")
        print("\n".join(_finish_lines(finishes)).strip("\n"))
        print()
        return 0

    print(f"\nDispatched runs in {home / 'runs'}\n")
    print(summary(runs, finishes))
    if args.per_task:
        print(f"\nBy task\n\n{per_task(runs)}")
    if args.list or args.task:
        print(f"\nRuns\n\n{listing(runs)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
