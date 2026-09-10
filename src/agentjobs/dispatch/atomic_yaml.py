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

What this deliberately does **not** fix is a lost update. Two writers that each read,
merge and replace can still have one merge win, because the read and the write are not
one operation -- and closing that needs a lock per run directory, which is a larger change
than the failure in evidence justifies. Nothing in the cancel path needs it: the writes
there are sequential within one thread, and the supervisor writes only when the flag it
reads is absent. Atomicity is what that guard was missing.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = ["read_shared", "read_yaml_resiliently", "write_yaml_atomically"]


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


READ_ATTEMPTS = 8
READ_POLL_SECONDS = 0.005
"""Retry budget for a read: about 40ms, and it exists because of the *other* half.

Replacing the file removed torn content and left one window behind: on Windows, while
``MoveFileExW`` swaps the target, a process opening it can be refused with
``ERROR_ACCESS_DENIED``. Python raises ``PermissionError``, which is an ``OSError`` --
and every reader in this subsystem was written to answer ``{}`` when the read fails.

So a transient refusal still arrives at a guard as *the flag is not set*, which is the
original defect wearing different clothes. Measured after the write fix, six readers
against two writers for six seconds: **1 to 3 reads in ~3700 still came back empty**, and
none of them was a file that was actually empty. Retrying removes them, because the
condition clears in microseconds.
"""


def read_yaml_resiliently(path: Path, *, loader: Any = None) -> Any:
    """The document at *path*, or ``None`` when it is genuinely not there.

    ``None`` is reserved for *absence*, and the whole point is that it now means only
    that. A file that exists but cannot be opened this instant is retried rather than
    reported as missing, and a caller that maps the result to ``{}`` is therefore mapping
    a real answer.

    A ``yaml`` parse failure is **not** retried and returns ``None``: with the write side
    replacing rather than rewriting, unparseable content is genuinely corrupt rather than
    half-written, and spending 40ms per call to re-read it would only slow down the one
    case where the answer will not change.

    ``loader`` selects the parser -- callers on a hot path pass ``storage.load_yaml``,
    which uses libyaml and is an order of magnitude faster on a run's argv blob.
    """
    parse = loader or yaml.safe_load
    for attempt in range(READ_ATTEMPTS):
        try:
            raw = read_shared(path)
        except FileNotFoundError:
            # Genuinely absent, and absence is not transient. Answer at once: a run
            # directory with no meta is a real state and every caller handles it.
            return None
        except OSError:
            if attempt == READ_ATTEMPTS - 1:
                return None
            time.sleep(READ_POLL_SECONDS)
            continue
        try:
            return parse(raw)
        except yaml.YAMLError:
            return None
    return None  # pragma: no cover - the loop returns on every path
