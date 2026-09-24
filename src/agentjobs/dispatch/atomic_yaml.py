"""Replacing a small YAML file so that no reader ever sees half of one.

**Every guard in the dispatch subsystem is a read of a file somebody else may be
writing.** A run's ``meta.yaml`` says whether a run is live, whether a cancellation has
been requested, whether an escalation is waiting on it -- and each of those is read by a
different process from the one that wrote it. Until task-390 all three writers did the
same thing:

.. code-block:: python

    meta_path.write_text(yaml.safe_dump(merged), encoding="utf-8")

which truncates the file and then writes it, and ``read_meta`` turns any failure to parse
what it finds into ``{}`` rather than an error. So a reader landing inside that window
does not get an exception it might have retried -- it gets a **plausible empty answer**,
and every ``meta.get(...)`` guard built on it silently reports absence.

Measured on 2026-09-07, two writers against six readers on one file for eight seconds:
**2886 reads, 0 of them complete** -- 1085 empty and 1796 missing keys that were never
removed. The window is not theoretical and it is not small.

What it cost: ``tests/test_dispatch_api.py::TestDispatchRuns::test_cancelling_a_live_run
_stops_it_and_marks_it_cancelled`` failing under the gate's xdist load with ``assert
'failed' == 'cancelled'``. A cancelled batch run's supervisor wakes to a non-zero exit,
reads ``meta.yaml`` to see whether the kill was a cancellation, gets a torn read, and
overwrites the cancellation with ``failed``. ``runner._finish_batch`` names that race in
its docstring and orders the writes to remove it; the ordering is right, and a torn read
defeats it anyway, because the flag it looks for cannot be seen.

**The fix is replacement rather than rewriting.** The document is built in a temporary
file beside the target and then moved onto it with :func:`os.replace`, which is atomic on
Windows and on POSIX: a reader opens either the whole old file or the whole new one,
never a prefix of either.

What replacement alone does **not** fix is a lost update. Two writers that each read,
merge and replace can still have one merge win, because the read and the write are not
one operation. Task-264 found the case that mattered: a poll tick writing ``status:
stalled`` from a read taken before a cancellation's terminal write, and landing after it
-- the run's meta then read live again while its task said cancelled.
:func:`merge_yaml_atomically` closes it with a short lock per file around the
read-merge-replace. That lock serialises writes to a *projection*; it decides nothing.
Who may write a terminal status is the execution journal's compare-and-set
(``dispatch.journal``), and the merge rule that keeps a terminal status terminal is the
caller's (``runner.finish_stamped``).
"""

from __future__ import annotations

import errno
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

import yaml

__all__ = [
    "DocumentUnreadable",
    "merge_yaml_atomically",
    "read_shared",
    "read_yaml_resiliently",
    "write_yaml_atomically",
]


# ----- reading without standing in the writer's way ---------------------------

_WINDOWS = os.name == "nt"

if _WINDOWS:  # pragma: no cover - the other branch is unreachable on this platform
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE

    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE_DELETE = 0x00000001 | 0x00000002 | 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def read_shared(path: Path) -> str:
    """Read *path* without preventing anybody from replacing it while we do.

    **This is what makes the atomic write above cheap on Windows**, and without it the
    two halves fight each other. ``os.replace`` needs delete access to its target, and
    Python's ``open`` asks for ``FILE_SHARE_READ | FILE_SHARE_WRITE`` and *not*
    ``FILE_SHARE_DELETE`` -- so on Windows an ordinary reader blocks a replace outright.
    Measured on 2026-09-07: four readers looping on one file held a writer off for
    hundreds of milliseconds per write, and with a backoff schedule starved 150 writes
    for two and a half seconds until they raised. That is a real cost in ``cancel()``,
    which does this write inside a synchronous request.

    Opening with ``FILE_SHARE_DELETE`` gives Windows the semantics POSIX already has and
    every reader in this subsystem was written assuming: the replace succeeds regardless
    of who is reading, and a handle already open keeps reading the file it opened. There
    is then no contention to wait for -- the retry loop in ``write_yaml_atomically``
    stays as a guard against a virus scanner or another tool holding the file, not as
    the normal path.

    On POSIX this is ``path.read_text``: a rename over an open file has always been
    invisible to the reader there, so there is nothing to arrange.

    Errors are raised as the ordinary Python exceptions, via ``ctypes.WinError``, which
    maps ``ERROR_FILE_NOT_FOUND`` to ``FileNotFoundError`` and ``ERROR_ACCESS_DENIED``
    to ``PermissionError``. Callers therefore need no platform knowledge.
    """
    if not _WINDOWS:
        return path.read_text(encoding="utf-8")

    handle = _CreateFileW(
        str(path),
        _GENERIC_READ,
        _SHARE_READ_WRITE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    # `open_osfhandle` transfers ownership of the handle to the file descriptor, so
    # closing the stream closes the handle and there is no separate CloseHandle.
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
        return stream.read()


REPLACE_BUDGET_SECONDS = 10.0
REPLACE_SPINS = 2000
REPLACE_POLL_SECONDS = 0.002
"""Retry budget for the move: spin first, then poll, for at most ten seconds.

**Sized from measurement, not from taste** (task-390, 2026-09-07, this machine).

On Windows ``os.replace`` fails with ``PermissionError`` while *any* handle is open on the
target, because Python's ``open`` does not ask for ``FILE_SHARE_DELETE``. So on this
platform every concurrent reader is contention for the writer, and a writer can in
principle be starved by readers that never all happen to be closed at the same instant.

**The shape of the retry matters more than its length.** What the writer is waiting for is
the microsecond between one reader's close and its next open, so it samples: 2000 yields
before it starts sleeping at all. A 5ms-and-rising backoff was tried first and missed that
gap for a full 2.5 seconds against four spinning readers -- 150 writes could not complete
-- where spinning finds it at once. The ten-second ceiling is a deadlock guard, not a
working budget; nothing observed has spent more than a millisecond in this loop.

Exhausting it **raises**, and that is deliberate. The alternative -- falling back to an
in-place rewrite -- would reintroduce the torn read silently and precisely under the load
that makes it likely, which is the failure this module exists to remove. A caller that
must not fail on a record write catches it and says so; ``FinishDirectory.write_meta``
is the one that does.
"""


def write_yaml_atomically(
    path: Path, data: Mapping[str, Any], *, allow_unicode: bool = False
) -> None:
    """Make *path* hold *data*, with no moment at which it holds something else.

    The temporary file is created **in the target's own directory**, because
    :func:`os.replace` is only atomic within a filesystem and a temp directory may be on
    another one. It carries the pid and a random suffix so two writers racing this never
    collide on the temporary either.

    ``PermissionError`` on the replace is retried rather than raised. On Windows a file
    whose last handle has closed but whose delete has not completed sits in a
    delete-pending state, and a virus scanner holding the target open for a few
    milliseconds looks the same; both are contention, and both clear. This is the same
    reasoning the file backend's task lock gave for retrying ``PermissionError``, and
    it was found the same way -- under real concurrency, not in a serial test.

    Raises whatever the filesystem raises if the write itself fails. Callers that must
    not fail on a record write -- ``FinishDirectory.write_meta`` is the one -- catch it
    themselves and say so; swallowing it here would hide a full disk from all of them.
    """
    rendered = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=allow_unicode)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        deadline = time.monotonic() + REPLACE_BUDGET_SECONDS
        spins = 0
        while True:
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                # Yield rather than sleep for the first burst. What this is waiting for
                # is the microsecond between one reader's close and its next open, and a
                # backoff samples too slowly to find it: measured on 2026-09-07, a
                # 5ms-and-rising schedule missed the gap for a full 2.5 seconds against
                # four spinning readers, while spinning found it immediately.
                if spins < REPLACE_SPINS:
                    spins += 1
                    time.sleep(0)
                else:
                    time.sleep(REPLACE_POLL_SECONDS)
    finally:
        # Only reached with the temporary still present when the replace never happened.
        # A leaked `.tmp` beside a run's meta is litter that outlives the run, and the
        # directory is read by `finish_status` and the runs listing.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - the directory went away underneath us
            pass


READ_BUDGET_SECONDS = 2.0
READ_POLL_SECONDS = 0.005
"""Retry budget for a read: two seconds of polling, measured in time, then a raise.

**Why a read retries at all** (task-390). Replacing the file removed torn content and left
one window behind: on Windows, while ``MoveFileExW`` swaps the target, a process opening
it can be refused with ``ERROR_ACCESS_DENIED``. Measured after the write fix, six readers
against two writers for six seconds: 1 to 3 reads in ~3700 were refused, none of them a
file that was actually empty. That condition clears in microseconds.

**Why the budget is time, and why running out of it raises** (task-550). The budget used
to be eight attempts, about 40ms, after which the read answered ``None`` -- *absent* --
for a file that was there. Something that holds the file for longer than that (a
real-time scanner opening a freshly replaced file exclusively is the likely candidate
here, since Defender's real-time protection is on) turned every read in the hold into
*the flag is not set*. Under a ``-n 26`` gate that was 461 of 1153 reads in one run of
``test_a_reader_never_sees_a_partial_document``. That is task-390's defect again, only
delayed by 40ms.

Two seconds is fifty times the old budget and still a bound, not a wait anyone should
see: a read refused for that long is a file nobody can read, and the honest answer then is
:class:`DocumentUnreadable`, not an empty document. Where the budget is spent it only
replaces an answer that was wrong. The write path is unchanged.
"""


class DocumentUnreadable(RuntimeError):
    """A file that exists could not be opened within :data:`READ_BUDGET_SECONDS`.

    **Deliberately not an** ``OSError``. The callers that catch ``OSError`` catch it
    to answer *missing*, and this is exactly the case that must not be reported as
    missing. A caller that has a safe answer for *unknown* catches this by name, or
    catches ``Exception``. ``controller._meta`` and ``journal._launch_marker`` already do.
    Otherwise it propagates, and the read fails loudly instead of reporting a flag as
    absent.
    """

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(
            f"{path} exists but could not be opened for {READ_BUDGET_SECONDS}s: {cause}"
        )
        self.path = path


def read_yaml_resiliently(path: Path, *, loader: Any = None) -> Any:
    """The document at *path*, or ``None`` when it is genuinely not there.

    ``None`` is reserved for *absence*, and the whole point is that it means only that. A
    file that exists but cannot be opened this instant is retried rather than reported as
    missing. If it still cannot be opened after :data:`READ_BUDGET_SECONDS`, this raises
    :class:`DocumentUnreadable` rather than answer ``None`` (task-550). So a caller
    that maps the result to ``{}`` is mapping a real answer, however long the reader was
    starved.

    A ``yaml`` parse failure is **not** retried and returns ``None``: with the write side
    replacing rather than rewriting, unparseable content is genuinely corrupt rather than
    half-written, and re-reading it would only slow down the one case where the answer
    will not change.

    ``loader`` selects the parser -- callers on a hot path pass ``storage.load_yaml``,
    which uses libyaml and is an order of magnitude faster on a run's argv blob.
    """
    parse = loader or yaml.safe_load
    deadline = time.monotonic() + READ_BUDGET_SECONDS
    while True:
        try:
            raw = read_shared(path)
        except FileNotFoundError:
            # Genuinely absent, and absence is not transient. Answer at once: a run
            # directory with no meta is a real state and every caller handles it.
            # `os.replace` is one rename on both platforms, and there is no instant at
            # which the name is unbound, so a replace does not look like this.
            return None
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise DocumentUnreadable(path, exc) from exc
            time.sleep(READ_POLL_SECONDS)
            continue
        try:
            return parse(raw)
        except yaml.YAMLError:
            return None


# ----- merging without losing a concurrent write -------------------------------

MERGE_LOCK_SUFFIX = ".lock"
MERGE_LOCK_BUDGET_SECONDS = 10.0
MERGE_LOCK_STALE_SECONDS = 30.0
"""A merge holds its lock for one read and one replace -- milliseconds. A lock older than
this was left by a writer that died mid-merge, whatever its recorded pid now names (pids
are reused), and is reclaimed. The budget bounds a wait behind a live one."""


def _merge_lock_is_stale(lock: Path) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age > MERGE_LOCK_STALE_SECONDS


def merge_yaml_atomically(
    path: Path,
    fields: Mapping[str, Any],
    *,
    merge: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]],
    loader: Any = None,
) -> Dict[str, Any]:
    """Read *path*, merge *fields* into it with *merge*, and replace it -- as one step.

    ``O_CREAT|O_EXCL`` on a sibling lock file, the primitive the run lock already uses, so
    there is no second locking convention to learn. Returns the document written.

    Exhausting the budget raises ``TimeoutError`` rather than merging unlocked: a merge
    that silently skipped the lock would reintroduce the lost update under exactly the
    contention that makes it likely.

    An unreadable current document raises :class:`DocumentUnreadable` and writes nothing,
    for the same reason (task-550). When the read answered ``None`` instead, the merge
    started from ``{}``, and the replace then erased every field it had not been handed.
    """
    lock = path.with_name(path.name + MERGE_LOCK_SUFFIX)
    deadline = time.monotonic() + MERGE_LOCK_BUDGET_SECONDS
    while True:
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            if _merge_lock_is_stale(lock):
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
        except OSError as exc:  # pragma: no cover - unexpected filesystem failure
            if exc.errno != errno.EEXIST:
                raise
        else:
            os.close(handle)
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"could not take the merge lock on {path} within budget")
        time.sleep(0)
    try:
        loaded = read_yaml_resiliently(path, loader=loader)
        current: Dict[str, Any] = dict(loaded) if isinstance(loaded, dict) else {}
        merged = merge(current, dict(fields))
        write_yaml_atomically(path, merged)
        return merged
    finally:
        try:
            lock.unlink()
        except OSError:  # pragma: no cover - reclaimed as stale underneath a slow writer
            pass
