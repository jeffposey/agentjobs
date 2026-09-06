"""How many cores one gate may ask for when it is not the only gate on the machine.

``PARALLEL_ARGS`` used to be ``-n auto``, which is pytest-xdist for *every core*. Task-233
measured that against a serial run and found 431s to 43s, and said so in its lever table:
"Concurrent parallel gates -- not measured. ``-n auto`` asks for every core, so two gates
now compete for the same 32." It filed nothing. This machine dispatches up to
``limits.max_concurrent_runs`` agents at once -- three -- so overlapping gates are not the
exception here, they are the normal case, and the 96s figure in docs/performance.md describes a
machine this project never runs on. Task-336's gates measured 231 / 250 / 289 / 397 / 451
seconds in the pytest stage against that documented 52.

**What runs out is memory, not cores**, which task-233 had no way to know: sampled on
2026-09-05, two concurrent gates at ``-n auto`` drove free memory on this 64GB machine to
**6MB**, twice, an hour apart, with the CPU at 32%. The suite also goes flaky rather than
merely slow there -- a timing assertion in ``TestProcessGroup`` failed in that arm and
cost the gate its whole run.

The fix is a budget rather than a queue. Every gate leaves a small file behind for as long
as it is running; the pytest stage divides the machine's cores by how many such files it
can see and asks for that many workers. Nobody waits and nothing is refused. The property
that matters is not fairness but the total: N gates at ``32/N`` workers each is 32 workers
whatever N is, so the machine-wide footprint of the pytest stage stays what one gate costs
-- 6.4GB and 1452MB free, against 10.7GB and 6MB.

**This is a reliability change and not a speed-up, and it is priced accordingly.**
Measured against a drift-controlled arm rather than against the earlier one -- the machine
got quieter over the evening, and the naive before/after read as a 14% win that was not
there -- it costs 5% of the pytest stage and 0.8% of the whole gate, which is inside
either arm's own spread. ``docs/performance.md`` has both arms and the control.

Four properties, each of which is a way this could have gone wrong:

**It never blocks and never fails the gate.** Every path here is wrapped by its caller so
that a broken slot directory costs the default -- ``-n auto``, exactly today's behaviour --
rather than a run. A budget that can stop a gate is worse than an oversubscribed one.

**Staleness is decided by age, not by liveness.** A killed gate cannot clean up after
itself, and asking Windows whether a pid is alive is both unreliable across users and a
way to mistake pid reuse for a running gate. A slot older than ``STALE_SECONDS`` is
ignored and deleted by whoever next reads the directory.

**The count is taken when pytest starts, not when the gate does.** The gate's cheap block
is seconds and its pytest stage is minutes, so a neighbour that starts while Black is
running would otherwise be invisible to the only stage that cares.

**Slots are per machine, not per checkout.** The resource being divided is this machine's
cores, and the gates competing for them are in different worktrees by construction.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

HOME_ENV = "AGENTJOBS_HOME"
SLOTS_DIRNAME = "gate-slots"

STALE_SECONDS = 30 * 60.0
"""When a slot file stops counting as a gate that is running.

Thirty minutes. The longest gate this machine has recorded is task-336's 581 seconds
under contention, and the walk's own children are bounded well below this, so no live
gate is ever mistaken for a corpse. The cost of the other error is what sets the ceiling:
a slot left by a killed gate throttles every gate that follows it until it expires, and
half an hour of half-speed suites is the most that is tolerable.
"""

MIN_WORKERS = 4
"""The floor, whatever the arithmetic says.

Three concurrent runs on this 32-core machine divide to ten. A machine with four cores
and three gates would divide to one, which is serial pytest -- 431s measured, ten times
the parallel figure -- so the division is floored rather than trusted. Below four workers
the suite is slow enough that the oversubscription it was avoiding is the cheaper problem.
"""


def slots_dir() -> Path:
    """Where this machine's gate slots live, beside the run ledger they belong with."""
    home = Path(os.environ.get(HOME_ENV) or (Path.home() / ".agentjobs"))
    return home / SLOTS_DIRNAME


def _read_slots(directory: Path, *, now: float) -> List[Path]:
    """The slot files that describe a gate plausibly still running.

    Expired ones are deleted on sight. Doing it here rather than in a sweeper means the
    directory is tidied by the next gate that has a reason to look at it, which is the
    only moment anyone cares whether it is tidy.
    """
    live: List[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return live
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > STALE_SECONDS:
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        live.append(entry)
    return live


def active(now: Optional[float] = None) -> int:
    """How many gates are running on this machine, this one included.

    Zero is returned when the directory cannot be read at all, and callers treat that the
    same way they treat one: no division, ``-n auto``, today's behaviour.
    """
    return len(_read_slots(slots_dir(), now=time.time() if now is None else now))


@contextmanager
def hold(root: Path) -> Iterator[None]:
    """Declare a gate running in ``root`` for the length of this block.

    The file is named by pid and a random token rather than by pid alone: a worktree can
    be gated twice by one shell in sequence, and a pid that is reused between them would
    otherwise silently reuse a slot.
    """
    path: Optional[Path]
    try:
        directory = slots_dir()
        path = directory / f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        payload = {"pid": os.getpid(), "root": str(root), "started_at": time.time()}
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - a slot is an optimisation; the gate is not
        # No slot means this gate is invisible to its neighbours and divides nothing
        # itself. That is the un-budgeted behaviour, which is a slow gate rather than a
        # broken one.
        path = None
    try:
        yield
    finally:
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def workers(cores: Optional[int] = None, gates: Optional[int] = None, reserve: int = 0) -> str:
    """The ``-n`` value for pytest-xdist, given how many gates share this machine.

    ``auto`` when this gate is alone and nothing is reserved, so the single-gate case is
    byte-for-byte what it was before task-339 and the measurements in docs/performance.md
    still describe it.

    ``reserve`` is cores this gate is holding back from its own suite, which only a
    ``--concurrent`` run does: there the frontend lane is running *inside* the same gate
    while pytest runs, and it is invisible to the slot count because it is not a gate.
    A reserve therefore turns ``auto`` into a number even when this is the only gate on
    the machine -- that is the whole point of asking for one.
    """
    if gates is None:
        gates = active()
    if gates <= 1 and reserve <= 0:
        return "auto"
    if cores is None:
        cores = os.cpu_count() or 1
    return str(max(MIN_WORKERS, cores // max(1, gates) - max(0, reserve)))


def note(value: str, gates: int, reserve: int = 0) -> Optional[str]:
    """Say out loud that the suite was given less than the machine, and why.

    A gate that quietly runs at half width is a gate whose timings nobody can compare
    with the table in docs/performance.md. Printed at the top of the pytest stage.
    """
    if value == "auto":
        return None
    held = f", {reserve} of them reserved for the frontend lane beside it" if reserve else ""
    return (
        f"Sharing this machine with {gates} gates, so pytest runs at -n {value} rather "
        f"than -n auto ({os.cpu_count()} cores){held}. See scripts/gate_slots.py."
    )
