"""What a recorded pid is worth, and what has to be true before one is acted on.

A pid is not a name for a process. It is a number the kernel lends out and takes back,
and this machine takes it back fast: 2000 short-lived children spawned eight at a time
on 2026-09-21 used **988 distinct pids**, with a dead number handed to a new process
after a median of 23.7 seconds and a minimum of 0.35 (task-505). The dispatch suite at
``-n 10`` spawns considerably faster than that, and three or four live agent sessions
spawn beside it.

So a pid written down a minute ago and believed today is a coin flip, and it fails in
both directions:

- **Believed alive.** A stranger inherits the number, the code waiting for the recorded
  process to end waits forever, and the run is never concluded. Observed as
  ``AssertionError: []`` from ``TestBatchRecovery`` -- a controller tick that found
  nothing to say because it thought the supervisor was still watching.
- **Killed.** ``taskkill /PID <pid> /T /F`` aimed at a recycled number kills whatever
  holds it now. A process killed that way **exits 1 with empty stdout and empty stderr**,
  measured 2026-09-21, which is exactly the signature task-505 was filed about and is not
  something a Python traceback can produce. Under ``-n 10`` the stranger is usually
  another xdist worker's child, which is why the victim was a different test every time.

**Two receipts, and which one a caller has decides which predicate it uses.**

``process_identity`` is exact: the creation time as the OS reports it, so a match is
proof and a mismatch is proof of the opposite. A caller that recorded one at spawn --
a dispatched batch worker does -- should use it.

``recorded_at`` is the weaker receipt, for a process that was *already running* when it
wrote the thing being read: the process that admitted an attempt, the supervisor that
started a run. Such a process cannot have been created after what it wrote, so a
creation time later than the record proves reuse. It cannot prove the converse, which is
why the two predicates below differ in what they do with doubt.

**And the direction of doubt is not the same for the two questions.** Asking "is it
still running" may safely answer yes when it cannot tell: that refuses a slot rather
than clearing one, and is what every caller did before task-505. Asking "may I kill
this" must answer **no** when it cannot tell, because the cost of the other error is
somebody else's process.

Nothing here imports from the rest of the package; ``dispatch.ledger`` and
``execution.store`` re-export the three names that were theirs before task-505, so the
rule has one implementation rather than three halves of one.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

REUSE_SLACK = timedelta(seconds=1)
"""Clock granularity allowance when comparing a creation time against a written moment.

A recycled pid is seconds to minutes newer than the record it is being compared with, so
a second of slack never hides one. Inherited from task-444, which is where the comparison
was first written.
"""


def _times(pid: int) -> Optional[Tuple[str, datetime]]:
    """This pid's creation stamp, as ``(identity, started_at)``, or ``None``.

    One OS read serving both receipts, so they can never disagree about a process. The
    identity string keeps the exact spelling task-416 wrote into run metadata --
    ``win:<pid>:<filetime ticks>`` and ``proc:<pid>:<starttime ticks>`` -- because
    receipts recorded by earlier versions are on disk and are compared as strings.

    ``None`` whenever the process is gone or the platform will not say, and every caller
    treats that as "cannot prove", never as an answer.
    """
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            import ctypes

            query_limited_information = 0x1000
            # getattr rather than ctypes.windll.kernel32: the attribute only exists on
            # Windows, so the direct spelling is a type error wherever mypy is not run
            # here.
            kernel32 = getattr(ctypes, "windll").kernel32
            handle = kernel32.OpenProcess(query_limited_information, False, pid)
            if not handle:
                return None
            try:
                created = ctypes.c_ulonglong()
                ignored = [ctypes.c_ulonglong() for _ in range(3)]
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(ignored[0]),
                    ctypes.byref(ignored[1]),
                    ctypes.byref(ignored[2]),
                ):
                    return None
            finally:
                kernel32.CloseHandle(handle)
            ticks = created.value
            started = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
                microseconds=ticks // 10
            )
            return f"win:{pid}:{ticks}", started

        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The command name is parenthesised and may contain spaces; fields follow its close.
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) <= 19:
            return None
        ticks_since_boot = int(fields[19])
        since_boot = ticks_since_boot / getattr(os, "sysconf")("SC_CLK_TCK")
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        started = datetime.fromtimestamp(boot + since_boot, tz=timezone.utc)
        return f"proc:{pid}:{ticks_since_boot}", started
    except Exception:  # noqa: BLE001 - an unreadable start time is "cannot tell"
        return None


def process_alive(pid: int) -> bool:
    """Whether a process with this id exists right now.

    ``os.kill(pid, 0)`` is the Unix idiom and is **not** used here: on Windows CPython
    implements ``os.kill`` with ``TerminateProcess`` for most signals, so the usual
    liveness probe is a kill wearing a question mark. Windows goes through
    ``OpenProcess`` and ``GetExitCodeProcess``.

    This answers about a *number*, not about a process, and on this machine the number
    is recycled in seconds. Anything comparing it against something written down wants
    :func:`recorded_process_alive`.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # exists, owned by somebody else
            return True
        return True

    import ctypes

    still_active = 259
    query_limited_information = 0x1000
    kernel32 = getattr(ctypes, "windll").kernel32
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell; treat as alive, which refuses rather than clears
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int) -> Optional[str]:
    """A string naming *this* process rather than whichever one holds its pid next.

    The process's creation time, which a reused pid does not share. ``None`` when the
    process is gone or the platform offers neither -- and a caller treats ``None`` as
    "cannot prove it is the same process", never as "it is" (task-416: no PID-only
    adoption).
    """
    times = _times(pid)
    return times[0] if times else None


def process_created_after(pid: int, moment: datetime) -> bool:
    """Whether the process now answering to ``pid`` was started after ``moment``.

    ``True`` is proof the pid was reused: a holder that recorded something at ``moment``
    was already running then. ``False`` whenever the start time cannot be read, so the
    answer only ever clears a holder on evidence and never on doubt (task-444).
    """
    times = _times(pid)
    return times is not None and times[1] > moment + REUSE_SLACK


def recorded_process_alive(
    pid: Optional[int],
    *,
    recorded_at: Optional[datetime] = None,
    identity: Optional[str] = None,
) -> bool:
    """Whether the process this pid was recorded for is still the one answering to it.

    The liveness question, so doubt answers **yes**: an unreadable creation time, or a
    caller with no receipt to offer, gets the bare ``process_alive`` answer, which is
    what every call site did before task-505. What this adds is the one case that can be
    settled on evidence -- a process that demonstrably started *after* the record was
    written, or whose identity contradicts a recorded one, is not the recorded process
    and its pid is not a reason to keep waiting.

    Give it ``identity`` where the caller recorded one at spawn and ``recorded_at``
    where the recorded process was already running when it wrote the record. Both are
    accepted, and either alone is enough to catch a recycled number.
    """
    if pid is None or not process_alive(int(pid)):
        return False
    if identity is not None:
        return process_identity(int(pid)) == identity
    if recorded_at is not None and process_created_after(int(pid), recorded_at):
        return False
    return True


def is_the_recorded_process(
    pid: Optional[int],
    *,
    recorded_at: Optional[datetime] = None,
    identity: Optional[str] = None,
) -> bool:
    """Whether this pid is *provably* the process that was recorded. Doubt answers no.

    The gate in front of anything that kills. ``recorded_process_alive`` may say yes
    when it cannot tell, because being wrong there costs a slot; being wrong here costs
    somebody else's process, so this refuses unless the OS actually produced a creation
    time and that time agrees with the record.

    A caller with no receipt at all gets ``False``, which is the point: a bare pid is
    never sufficient authority to kill. Code that holds an open OS handle on the process
    is exempt and does not come through here -- the handle is what stops the number
    being reused, so a ``Popen`` the caller still owns needs no proof.

    Liveness is part of the proof rather than a separate question, and for a reason that
    only shows up on Windows: a process that has exited still answers ``OpenProcess``
    and still reports its creation time for as long as anybody holds a handle on it, so
    the receipt alone would match a corpse. There is nothing there to kill.
    """
    if pid is None or int(pid) <= 0 or not process_alive(int(pid)):
        return False
    times = _times(int(pid))
    if times is None:
        return False
    if identity is not None:
        return times[0] == identity
    if recorded_at is not None:
        return times[1] <= recorded_at + REUSE_SLACK
    return False
