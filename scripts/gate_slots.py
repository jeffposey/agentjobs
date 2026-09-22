"""How many gates may run pytest at once on this machine, and how wide each may run.

Task-339 made this a budget: every gate leaves a file behind, the pytest stage divides
the machine by how many such files it can see, and nobody waits. That held the
machine-wide worker count at one gate's however many were running, which is what bounded
the memory -- two concurrent gates at ``-n auto`` had driven free memory on this 64GB
machine to **6MB**, twice, an hour apart, at 32% CPU.

**A pure budget is the wrong shape below about ten workers, and task-513 measured why.**
The suite is nearly flat from 32 workers to 16 -- 8% for half the machine -- and steep
underneath: a lone gate throttled to what a fifth gate would be handed ran **3.0x** a
lone gate at ``-n auto``, with no contention anywhere in the measurement. So dividing
without a ceiling does not share the machine, it makes every gate slow at once; three
gates at ten workers is the regime that produced the 45-minute gates this task was filed
on. Task-536 therefore adds the three things a budget alone cannot express:

**A capacity of two** (``CAPACITY``). Two gates at thirteen workers each sit on the flat
part of that curve, so each costs close to what it costs alone, and a third run waits one
gate rather than two. It queues visibly, in the shape the merge runway uses -- takeoff
and landing are different resources, and so are a gate's cores.

**An owner's reserve** (``OWNER_RESERVE``). ``workers()`` never returns ``auto`` again.
Six of this machine's cores are not the gate's to take, whatever else is running, because
the person who owns the machine is using it while agents gate on it.

**A heartbeat** (``HEARTBEAT_SECONDS``). Staleness used to be thirty minutes and the
gates that caused this task ran forty-five, so a gate outlived its own slot, vanished
from the count, and invited a third that then made all three slower still -- a pileup
that feeds itself. The holding process now touches its slot every minute and staleness
drops to five, which means a killed gate frees its slot within five minutes and a live
one is never mistaken for a corpse however long it runs.

The numbers here are a choice from task-513's existing curve rather than a measurement of
their own; task-534 measures against them. If a different shape wins there, the numbers
change and this mechanism stays.

Five properties, each of which is a way this could have gone wrong:

**It never fails a gate.** Every path is wrapped: a broken slot directory, a lock file
that cannot be created, a heartbeat thread that dies. Each costs speed and never the run.
A budget that can stop a gate is worse than an oversubscribed one.

**The wait is bounded.** ``QUEUE_TIMEOUT_SECONDS`` is forty minutes, after which a queued
gate says so and proceeds beside the others at the two-gate share. A budget that can hold
a gate forever is a new way to be stuck (task-190).

**Staleness is decided by age, not by liveness.** A killed gate cannot clean up after
itself, and asking Windows whether a pid is alive is both unreliable across users and a
way to mistake pid reuse for a running gate. Liveness is the heartbeat and nothing else.

**The count is taken when pytest starts, not when the gate does.** The gate's cheap block
is seconds and its pytest stage is minutes, so a neighbour that starts while Black is
running would otherwise be invisible to the only stage that cares.

**Slots are per machine, not per checkout.** The resource being divided is this machine's
cores, and the gates competing for them are in different worktrees by construction.

Only a *parallel pytest* takes a slot -- the gate's pytest stage, or a hand-run
``pytest -n auto`` through the hook in ``tests/conftest.py``. A gate that does not select
pytest takes nothing and waits for nothing, so the ``--only oxlint`` loop that
ENGINEERING.md asks for is never queued behind two suites; and neither is a serial
``pytest -k one_test``, which costs one core.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional

HOME_ENV = "AGENTJOBS_HOME"
SLOTS_DIRNAME = "gate-slots"

SLOT_ENV = "AGENTJOBS_GATE_SLOT"
"""Set by whoever holds a slot, so the pytest inside a gate does not take a second one.

``tests/conftest.py`` implements xdist's ``pytest_xdist_auto_num_workers`` hook, which
makes a hand-run ``pytest -n auto`` obey this budget -- and that same hook fires inside
the gate's own pytest stage, where the gate is already holding a slot on its behalf. The
environment is how the two recognise each other: it is inherited by the subprocess the
gate launches and by nothing else on the machine.
"""

CAPACITY = 2
"""How many gates may run pytest at once here. A third queues.

The owner asked for one at thirty or three at ten. Neither, on the curve task-513
measured: three at ten sits at its knee, each gate 1.5 to 2x its lone cost, and that is
the regime the 45-minute gates came from; one at thirty gives the best per-gate time and
the deepest queue, since with the run ceiling at three the third run waits two whole
gates. Two at thirteen stays on the flat part -- each gate close to its lone cost -- and
the third run waits one gate. Memory is inside what task-339 measured for two at sixteen:
6.4 GB peak, 1452 MB lowest free.
"""

CAPACITY_ENV = "AGENTJOBS_GATE_CAPACITY"
"""Overrides ``CAPACITY`` for one process tree. For measuring, not for getting past it.

``scripts/gate_cost.py`` exists to run a gate at a worker count it would not otherwise be
handed -- that is how task-513 separated "the budget is throttling us" from "the machine
is contended". Under a capacity it cannot do that at all: its third synthetic slot would
queue, and so would the ``check.py`` it drives. So the capacity is a default rather than
a constant, and the tool that needs to step outside it says so in an environment
variable its own subprocess inherits.

Nothing else sets it. A gate that finds it set in an inherited environment is a gate
whose neighbours it is no longer bounded against, which is why the queue and overrun
notices name the capacity they actually used.
"""

OWNER_RESERVE = 6
"""Cores that are never the suite's, however many gates are running.

``workers()`` used to return ``auto`` for a lone gate, which is pytest-xdist for *every
core* -- and this machine is somebody's desktop while it is an agent's build server. Six
is the smallest reserve that leaves a browser, an editor and a shell usable through a
gate; it costs a lone gate 32 workers against 26, which is inside the flat part of
task-513's curve where half the machine cost 8%.
"""

MIN_WORKERS = 4
"""The floor, whatever the arithmetic says.

A machine with four cores and two gates would divide to a negative number. Below four
workers the suite is slow enough -- 431s serial against 43s parallel, measured by
task-233 -- that the oversubscription being avoided is the cheaper problem. On a machine
smaller than ``OWNER_RESERVE`` this is the only number that ever applies.
"""

STALE_SECONDS = 5 * 60.0
"""When a slot file stops counting as a gate that is running.

Five minutes, because the holder touches it every ``HEARTBEAT_SECONDS``. Before task-536
this was thirty minutes and had to cover the longest gate anyone could imagine, which is
exactly the assumption that failed: the gates that caused this task ran forty-five, so
they disappeared from the count while still running and the next gate took a larger share
of a machine that did not have it. With a heartbeat the ceiling no longer has to guess at
a gate's length -- it only has to outlast a missed touch or two -- so it can be short
enough that a killed gate stops throttling its neighbours within minutes.
"""

HEARTBEAT_SECONDS = 60.0
"""How often the holding process touches its own slot file.

A daemon thread, so it cannot keep a process alive and cannot fail a gate: if it dies,
the slot expires as it would have before and the machine is merely divided by one fewer.
Five touches fit inside ``STALE_SECONDS``, which is the margin for a machine paging hard
enough that a thread does not get scheduled.
"""

QUEUE_NOTICE_SECONDS = 30.0
"""How often a queued gate says who it is waiting for.

Immediately, then every thirty seconds. A gate that prints nothing for twenty minutes is
indistinguishable from a hung one, and the cost of that confusion is a person killing a
run that was working. The merge runway's ``runway_queued`` note is the precedent.
"""

QUEUE_TIMEOUT_SECONDS = 40 * 60.0
"""How long a gate queues before proceeding anyway.

Two suites ahead of it is roughly fifteen minutes on this machine, so forty is generous
rather than tight -- it is the point at which the slot directory has more likely gone
wrong than the queue has been long. Proceeding is never refusing: the gate runs, at the
two-gate share, and says in its own output that it did.
"""

POLL_SECONDS = 2.0
"""How often a queued gate re-reads the slot directory. Cheap; a handful of stats."""

LOCK_NAME = "acquire.lock"
LOCK_STALE_SECONDS = 30.0
LOCK_ATTEMPTS = 20
LOCK_RETRY_SECONDS = 0.1


def capacity() -> int:
    """``CAPACITY``, unless ``CAPACITY_ENV`` overrides it with a sane number."""
    raw = os.environ.get(CAPACITY_ENV)
    if not raw:
        return CAPACITY
    try:
        asked = int(raw)
    except (TypeError, ValueError):
        return CAPACITY
    return asked if asked >= 1 else CAPACITY


def slots_dir() -> Path:
    """Where this machine's gate slots live, beside the run ledger they belong with."""
    home = Path(os.environ.get(HOME_ENV) or (Path.home() / ".agentjobs"))
    return home / SLOTS_DIRNAME


@dataclass(frozen=True)
class Holder:
    """One gate that currently holds a slot, as its own file describes it."""

    path: Path
    pid: int
    root: str
    started_at: float

    def describe(self) -> str:
        return f"{self.root} (pid {self.pid})"

    def since_phrase(self, now: Optional[float] = None) -> str:
        if not self.started_at:
            return ""
        seconds = max(0.0, (time.time() if now is None else now) - self.started_at)
        if seconds < 90:
            return f", {seconds:.0f}s in"
        return f", {seconds / 60:.0f}m in"


@dataclass(frozen=True)
class Survey:
    """What the slot directory says right now, and whether it could be read at all."""

    holders: tuple[Holder, ...] = ()
    readable: bool = True

    @property
    def gates(self) -> int:
        return len(self.holders)


def _load(entry: Path) -> Holder:
    """A slot file as a :class:`Holder`, tolerating a file written by an older gate."""
    payload: dict = {}
    try:
        raw = json.loads(entry.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            payload = raw
    except (OSError, ValueError):
        pass
    try:
        pid = int(payload.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    try:
        started_at = float(payload.get("started_at", 0.0))
    except (TypeError, ValueError):
        started_at = 0.0
    return Holder(
        path=entry,
        pid=pid,
        root=str(payload.get("root", "an unknown checkout")),
        started_at=started_at,
    )


def survey(now: Optional[float] = None, directory: Optional[Path] = None) -> Survey:
    """The gates plausibly still running, and whether the directory could be read.

    Expired slots are deleted on sight. Doing it here rather than in a sweeper means the
    directory is tidied by the next gate that has a reason to look at it, which is the
    only moment anyone cares whether it is tidy.

    ``readable`` is the distinction a capacity needs and a pure budget did not: a
    directory that has never existed means no gate is running, while one that cannot be
    read means nothing at all, and the two must not divide by the same number.
    """
    moment = time.time() if now is None else now
    directory = slots_dir() if directory is None else directory
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return Survey((), True)
    except OSError:
        return Survey((), False)
    except ValueError:
        # An embedded NUL in the configured home: unreadable, not empty.
        return Survey((), False)
    live: List[Holder] = []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            age = moment - entry.stat().st_mtime
        except OSError:
            continue
        if age > STALE_SECONDS:
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        live.append(_load(entry))
    return Survey(tuple(live), True)


def active(now: Optional[float] = None) -> int:
    """How many gates are running on this machine, this one included."""
    return survey(now=now).gates


_DEGRADED = False
"""Set when this process wanted a slot and could not take one.

A gate with no slot is invisible to its neighbours, so it cannot count itself and neither
can they. The honest answer to "how many gates are there" is then not zero but *as many
as there may be*, which is why :func:`visible_gates` returns ``CAPACITY`` here -- the
gate runs at the two-gate share and says so, rather than at the whole machine. It is
process-wide rather than passed around because the two places that care -- taking the
slot, and resolving ``-n`` minutes later -- have no call path between them.
"""


def reset_degraded() -> None:
    """Forget a failed acquire. For tests, which share a process with the next test."""
    global _DEGRADED
    _DEGRADED = False


def visible_gates(now: Optional[float] = None) -> int:
    """The number to divide this machine by, never below one and never a guess downwards.

    ``CAPACITY`` when the directory cannot be read or this process failed to take a slot,
    because both mean the count is unknown and the cost of guessing low is two gates at
    26 workers each on a machine that has 32.
    """
    if _DEGRADED:
        return capacity()
    found = survey(now=now)
    if not found.readable:
        return capacity()
    return max(1, found.gates)


def budget(cores: Optional[int] = None, gates: Optional[int] = None, reserve: int = 0) -> int:
    """How many xdist workers this gate may ask for.

    ``(cores - OWNER_RESERVE) // min(gates, CAPACITY)``, floored at ``MIN_WORKERS``: 26
    alone and 13 paired on this 32-core machine. ``min`` rather than the raw count
    because a third gate is a queue rather than a third share -- and when one proceeds
    anyway after ``QUEUE_TIMEOUT_SECONDS``, it takes the same thirteen its neighbours
    have rather than a ninth of the machine.

    ``reserve`` is cores this gate is holding back from its own suite, which only a
    ``--concurrent`` run does: there the frontend lane runs *inside* the same gate while
    pytest runs, and is invisible to the slot count because it is not a gate. It comes
    off after the division, so it is a fixed cost to this gate rather than a share.
    """
    if gates is None:
        gates = visible_gates()
    if cores is None:
        cores = os.cpu_count() or 1
    share = max(1, min(gates, capacity()))
    return max(MIN_WORKERS, (cores - OWNER_RESERVE) // share - max(0, reserve))


def workers(cores: Optional[int] = None, gates: Optional[int] = None, reserve: int = 0) -> str:
    """:func:`budget` as the ``-n`` value pytest-xdist is given.

    Never ``auto``. That was the single-gate case before task-536, and it is what handed
    the owner's own machine to the suite.
    """
    return str(budget(cores=cores, gates=gates, reserve=reserve))


def note(value: str, gates: int, reserve: int = 0) -> str:
    """Say out loud what the suite was given and what was held back from it.

    Printed at the top of the pytest stage, every time rather than only when the budget
    bit: a timing in the gate's own table is meaningless unless it can be read against
    the width it ran at, and "the width was every core" is no longer a safe assumption.
    """
    cores = os.cpu_count() or 1
    company = "alone on this machine" if gates <= 1 else f"sharing this machine with {gates} gates"
    held = f", and {reserve} more for the frontend lane beside it" if reserve else ""
    return (
        f"pytest runs at -n {value}, {company}: {cores} cores less {OWNER_RESERVE} "
        f"reserved for the owner{held}. See scripts/gate_slots.py."
    )


def _lock_is_stale(lock: Path, *, now: float) -> bool:
    try:
        return now - lock.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return False


@contextmanager
def _exclusive(directory: Path) -> Iterator[bool]:
    """Hold the acquire lock, or yield ``False`` if it cannot be had.

    Counting the slots and creating one are two operations, and two gates arriving
    together would otherwise both see the same free slot and both take it -- the one race
    a capacity has that a pure budget did not. ``O_EXCL`` is the whole mechanism: atomic
    on every filesystem this runs on, needing no library, and leaving a file whose age
    says whether the holder died mid-acquire.

    Failing to take it is not failing. The caller proceeds unlocked, which is exactly the
    behaviour of a machine with no lock at all, and the worst that can produce is the
    third gate this file exists to make rare.
    """
    lock = directory / LOCK_NAME
    handle: Optional[int] = None
    for attempt in range(LOCK_ATTEMPTS):
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _lock_is_stale(lock, now=time.time()):
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if attempt < LOCK_ATTEMPTS - 1:
                time.sleep(LOCK_RETRY_SECONDS)
            continue
        except (OSError, ValueError):
            break
    if handle is None:
        yield False
        return
    try:
        try:
            os.write(handle, f"{os.getpid()}\n".encode())
        finally:
            os.close(handle)
    except OSError:
        pass
    try:
        yield True
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _create(directory: Path, root: Path) -> Optional[Path]:
    """Write this gate's slot file. ``None`` if it cannot be written at all.

    Named by pid *and* a random token rather than by pid alone: a worktree can be gated
    twice by one shell in sequence, and a pid reused between them would otherwise
    silently reuse a slot.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        payload = {"pid": os.getpid(), "root": str(root), "started_at": time.time()}
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - a slot is an optimisation; the gate is not
        return None


def queued_note(holders: tuple[Holder, ...], waited: float, ceiling: Optional[int] = None) -> str:
    """What a queued gate prints, in the shape the merge runway's queue note takes."""
    who = "; ".join(f"{holder.describe()}{holder.since_phrase()}" for holder in holders)
    waited_phrase = "" if waited < 1 else f" Waiting {waited / 60:.0f}m so far."
    room = capacity() if ceiling is None else ceiling
    return (
        f"\nQueued for one of this machine's {room} gate slots, held by: {who}."
        f"{waited_phrase}\n"
        f"**This is the queue working, not a stall.** Three suites at a third of the "
        f"machine each are slower than two at half and one waiting -- task-513 measured "
        f"the curve. Nothing runs here until a slot comes free, and this gate says who "
        f"it is waiting for every {QUEUE_NOTICE_SECONDS:.0f}s.\n"
    )


def overran_note(holders: tuple[Holder, ...], waited: float) -> str:
    """What a gate prints when it stops queueing and goes anyway."""
    who = "; ".join(holder.describe() for holder in holders) or "gates it can no longer read"
    return (
        f"\nWaited {waited / 60:.0f}m for a gate slot and stopped waiting. Proceeding "
        f"beside {who}, at the share a paired gate gets rather than a smaller one.\n"
        f"A budget that can hold a gate forever is a new way to be stuck (task-190), so "
        f"this is deliberate rather than a failure. If it happens twice, suspect the "
        f"slot directory: {slots_dir()}.\n"
    )


@dataclass
class Slot:
    """A held gate slot, and the thread keeping it warm.

    ``path`` is ``None`` when no slot could be taken. That is not an error state the
    caller handles -- it is the un-budgeted machine, which is a slow gate rather than a
    broken one -- so everything here is safe to call either way.
    """

    path: Optional[Path] = None
    waited: float = 0.0
    stop: Optional[threading.Event] = field(default=None, repr=False)
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    restore: Optional[str] = field(default=None, repr=False)
    owns_env: bool = field(default=False, repr=False)

    def release(self) -> None:
        if self.stop is not None:
            self.stop.set()
        if self.owns_env:
            if self.restore is None:
                os.environ.pop(SLOT_ENV, None)
            else:
                os.environ[SLOT_ENV] = self.restore
            self.owns_env = False
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.path = None


def _beat(path: Path, stop: threading.Event, interval: float) -> None:
    """Touch the slot file until told to stop, or until touching stops working.

    A thread that gives up is the same outcome as one that was never started: the slot
    ages out and the machine is divided by one fewer gate than there really are. That is
    why this swallows everything -- there is nothing it could usefully report to, and a
    daemon thread raising at interpreter shutdown is noise on a gate that passed.
    """
    while not stop.wait(interval):
        try:
            os.utime(path, None)
        except Exception:  # noqa: BLE001 - see the docstring
            return


def acquire(
    root: Path,
    *,
    ceiling: Optional[int] = None,
    timeout: float = QUEUE_TIMEOUT_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
    announce: Optional[Callable[[str], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Slot:
    """Wait for room, then take a slot and start its heartbeat.

    Returns rather than raises on every failure, and the returned :class:`Slot` is always
    safe to release. The keyword arguments exist so the wait can be driven in a test
    without a real clock; no caller in this repository passes them.
    """
    global _DEGRADED
    say = (lambda line: print(line, flush=True)) if announce is None else announce
    room = capacity() if ceiling is None else max(1, ceiling)
    began = clock()
    path: Optional[Path] = None
    try:
        directory = slots_dir()
        announced_at: Optional[float] = None
        while True:
            found = survey()
            if not found.readable:
                # Nothing can be counted, so nothing can be queued for. Try once for a
                # slot and go: waiting on a directory that cannot be read would wait the
                # whole timeout every time.
                path = _create(directory, root)
                break
            if found.gates < room:
                tried = False
                with _exclusive(directory) as locked:
                    # Re-read under the lock: between the survey above and this line a
                    # neighbour may have taken the last slot. Unlocked, the re-read is
                    # the best available and is still better than not looking.
                    if survey().gates < room or not locked:
                        tried = True
                        path = _create(directory, root)
                if tried:
                    # Either it worked, or the directory cannot be written -- and the
                    # second is not something waiting fixes. Queueing on it would spend
                    # the whole timeout to reach the same answer.
                    break
            waited = clock() - began
            if waited >= timeout:
                say(overran_note(survey().holders, waited))
                path = _create(directory, root)
                break
            if announced_at is None or clock() - announced_at >= QUEUE_NOTICE_SECONDS:
                announced_at = clock()
                say(queued_note(survey().holders, waited, room))
            sleep(POLL_SECONDS)
    except Exception:  # noqa: BLE001 - a slot is an optimisation; the gate is not
        path = None

    slot = Slot(path=path, waited=max(0.0, clock() - began))
    _DEGRADED = path is None
    if path is None:
        return slot

    try:
        slot.restore = os.environ.get(SLOT_ENV)
        os.environ[SLOT_ENV] = str(path)
        slot.owns_env = True
    except Exception:  # noqa: BLE001 - an environment that cannot be written is not a gate failure
        pass
    try:
        stop = threading.Event()
        thread = threading.Thread(
            target=_beat, args=(path, stop, heartbeat), name="gate-slot-heartbeat", daemon=True
        )
        thread.start()
        slot.stop = stop
        slot.thread = thread
    except Exception:  # noqa: BLE001 - see `_beat`; a missing heartbeat costs only the slot
        pass
    return slot


def held_by_an_enclosing_gate() -> bool:
    """Whether something around this process already holds a slot on its behalf.

    The gate's own pytest stage is the case: it runs inside a held slot, and the hook in
    ``tests/conftest.py`` must resolve a worker count there without taking a second one.
    """
    return bool(os.environ.get(SLOT_ENV))


@contextmanager
def hold(root: Path, *, needed: bool = True, **options: object) -> Iterator[None]:
    """Declare a gate running in ``root`` for the length of this block.

    ``needed`` is false for a run that will not start a parallel pytest -- an
    ``--only oxlint`` iteration, or ``--serial``. Such a run takes no slot and queues for
    nothing: it is not what the capacity is protecting, and queueing it behind two suites
    would punish exactly the loop ENGINEERING.md asks people to iterate in.
    """
    if not needed:
        yield
        return
    slot = acquire(root, **options)  # type: ignore[arg-type]
    try:
        yield
    finally:
        slot.release()
