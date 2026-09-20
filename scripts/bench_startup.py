"""What a restart costs, and how long the port is dead while it is paid.

``bench.py`` measures a server that is already up. This measures getting one up, which
is a different question with a different corpus: the run history under the AgentJobs
home, not the task store. Startup housekeeping walks that history, so its cost grows
with every dispatched run and nothing about a seeded task corpus exercises it.

Two modes, both repeatable and neither touching the machine's real home unless asked:

``phases``
    Times the pieces of the lifespan's startup half in this process, against a home
    this script seeds -- or, with ``--home``, against one that already exists, which is
    read-only. It reports what the reap *would* spawn rather than spawning it.

``handover``
    The end-to-end number: start a server, then stop it and start another, sampling
    which process owns the listening socket throughout. What it reports is the window
    in which nothing was listening -- the thing a client riding through a restart
    actually experiences.

Taking a before/after pair is two runs of the same command from two checkouts, because
the thing being changed is this repository's own startup path::

    git worktree add ../worktrees/agentjobs-before <commit-before>
    cp scripts/bench_startup.py ../worktrees/agentjobs-before/scripts/
    python scripts/bench_startup.py handover        # from each checkout, same flags

The copy is deliberate and the script is written to survive it: a benchmark has to be
newer than the code it measures, so what it reaches for in the application is guarded
and falls back to the older shape. Comparing two *different* scripts would compare the
scripts.

The defaults model this machine's ledger as it stood on 2026-09-20: 330 run
directories, 274 of them sessions already reaped, 31 sessions still to attempt and 20
of those permanently doomed -- one run per task, which is what the wake lookup was
quadratic in. A run with no flags is comparable with the figures in
docs/performance.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STARTUP_PORT_ENV = "AGENTJOBS_STARTUP_BENCH_PORT"
STARTUP_PORT_BASE = 41000
STARTUP_PORT_SPAN = 9000

DEFAULT_RUNS = 330
DEFAULT_SESSIONS = 305
DEFAULT_REAPED = 274
DEFAULT_DOOMED = 20
"""311 sessions were on the machine, six of them live or kept for an open task. Seeding
305 puts the number that matters -- the 31 reaps a start would attempt, 20 of them
doomed -- where it was observed on 2026-09-20."""

SERVER_START_TIMEOUT = 420.0
"""Generous on purpose. The number being measured is how long a start takes, so a
timeout tight enough to be a useful guard is also tight enough to hide the defect."""

SAMPLE_INTERVAL = 0.1


def checkout_port(root: Path) -> int:
    """The port this checkout's startup benchmark owns.

    Derived from the path for the same reason ``bench.py`` and the Playwright config
    derive theirs (task-187): several worktrees of this repository are worked at once,
    and a constant means the second run fails to bind. A different base and span from
    either of those, so three kinds of run in one checkout cannot collide.
    """
    override = os.environ.get(STARTUP_PORT_ENV, "").strip()
    if override:
        port = int(override)
        if not 1 <= port <= 65535:
            raise SystemExit(f"{STARTUP_PORT_ENV} must be between 1 and 65535, got {port}.")
        return port
    digest = hashlib.sha256(f"{root}\nstartup".encode()).digest()
    return STARTUP_PORT_BASE + (int.from_bytes(digest[:4], "big") % STARTUP_PORT_SPAN)


# ---------------------------------------------------------------------------------
# A run history to start against
# ---------------------------------------------------------------------------------


def seed_home(home: Path, *, runs: int, sessions: int, reaped: int, doomed: int) -> None:
    """Write a run history shaped like a real one, and nothing else.

    Every session id is spelled ``bench503-*`` so that the reap's ``rm`` calls cannot
    name a real conversation. The session manager answers "no job matching" to all of
    them, which is exactly the refusal the doomed population models -- so this seeds the
    defect rather than simulating it.
    """
    root = home / "runs"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    started = datetime.now(timezone.utc) - timedelta(days=30)
    for index in range(runs):
        directory = root / f"run_bench{index:04d}"
        directory.mkdir()
        is_session = index < sessions
        lines = [
            f"run_id: run_bench{index:04d}",
            # One task per run, as the real ledger largely is: the pair count is what
            # the wake lookup was quadratic in.
            f"task_id: task-{index:04d}",
            "project_id: bench-startup",
            f"mode: {'session' if is_session else 'batch'}",
            "status: finished",
            "outcome: completed",
            f"started_at: '{(started + timedelta(minutes=index)).isoformat()}'",
            f"finished_at: '{(started + timedelta(minutes=index + 5)).isoformat()}'",
        ]
        if is_session:
            lines.append(f"session_id: bench503-{index:04d}")
        if is_session and index < reaped:
            lines.append("reaped: true")
        elif is_session and index < reaped + doomed:
            lines.append(f"reap_blocked: No job matching 'bench503-{index:04d}'")
        (directory / "meta.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------
# Mode: phases
# ---------------------------------------------------------------------------------


def _timed(label: str, call) -> Tuple[str, float, object]:
    start = time.perf_counter()
    value = call()
    return label, time.perf_counter() - start, value


def run_phases(home: Path, *, seeded: bool) -> int:
    """Time the startup path's pieces against ``home``. Spawns no session manager.

    ``seeded`` is False for a home this script did not write, and it turns
    reconciliation off. Reconciliation is not a read: it settles runs a previous
    process left behind, which against the machine's real home would conclude runs that
    are alive -- including, quite possibly, the one measuring. Nothing else here writes,
    so that one omission is what makes ``--home`` safe to point anywhere.
    """
    os.environ["AGENTJOBS_HOME"] = str(home)

    rows: List[Tuple[str, float, object]] = []
    rows.append(_timed("import agentjobs.api.main", _import_app))

    from agentjobs.api.main import _verify_served_source, app
    from agentjobs.dispatch.ledger import DispatchLedger, list_runs
    from agentjobs.environment import capture_source_identity

    from agentjobs.api.contract import live_contract_digest

    rows.append(_timed("_verify_served_source()", _verify_served_source))
    rows.append(_timed("capture_source_identity()", capture_source_identity))
    rows.append(_timed("live_contract_digest()", lambda: live_contract_digest(app)))
    rows.append(_timed("list_runs()", lambda: list_runs(home)))

    records = rows[-1][2]
    ledger = DispatchLedger(home)
    if seeded:
        rows.append(_timed("DispatchLedger.reconcile()", ledger.reconcile))
    rows.append(_timed("_wakeable_run_ids()", lambda: _wakeable(ledger, records)))

    print(f"home {home}")
    if not seeded:
        print("reconcile skipped: it writes, and this home is not this script's to write to")
    print(f"{'step':38} {'seconds':>9}")
    for label, seconds, _value in rows:
        print(f"{label:38} {seconds:>9.2f}")
    print(f"{'TOTAL':38} {sum(row[1] for row in rows):>9.2f}")

    attempts, settled = _reap_shape(home, ledger, records)
    print()
    print(f"run directories:                {len(records)}")  # type: ignore[arg-type]
    print(f"session managers the reap would spawn: {attempts}")
    print(f"  of which already settled (skipped):  {settled}")
    return 0


def _import_app() -> None:
    import agentjobs.api.main  # noqa: F401


def _wakeable(ledger, records):
    """``_wakeable_run_ids``, whichever signature this checkout's ledger has.

    It takes the records to scan since task-503. An older checkout's re-reads them, and
    that re-read is a large part of what the pair is measuring -- so this passes them
    when it can and lets the old one do its own thing when it cannot.
    """
    try:
        return ledger._wakeable_run_ids(records)
    except TypeError:
        return ledger._wakeable_run_ids()


def _reap_shape(home: Path, ledger, records) -> Tuple[int, int]:
    """How many spawns the reap would issue, and how many it now skips as settled.

    Counted rather than performed: this mode is meant to be safe to point at a real
    home, and a reap against one deletes conversations.
    """
    try:
        from agentjobs.dispatch.ledger import _reap_is_settled
    except ImportError:
        # Before task-503 the only skip condition was `reaped: true`, which is why a
        # doomed run was re-attempted for ever. Spelling the old rule here is what lets
        # the older arm of a before/after pair be measured by this same script.
        from agentjobs.dispatch.ledger import META_FILENAME, _read_text

        def _reap_is_settled(record) -> bool:  # type: ignore[misc]
            meta_path = record.path / META_FILENAME
            return meta_path.is_file() and "reaped: true" in _read_text(meta_path)

    keep = _wakeable(ledger, records)
    attempts = 0
    settled = 0
    for record in records:
        if not record.is_session or record.is_live or record.run_id in keep:
            continue
        if _reap_is_settled(record):
            settled += 1
        else:
            attempts += 1
    return attempts, settled


# ---------------------------------------------------------------------------------
# Mode: handover
# ---------------------------------------------------------------------------------

_NETSTAT_ROW = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$")


def listening_pid(port: int) -> Optional[int]:
    """Which process owns the listening socket on ``port``, or ``None`` for nobody.

    The spec's own instrument (task-503). Connectability alone cannot tell a successor
    that has bound from a predecessor that has not let go, and during the early part of
    a restart it is the *old* process still answering -- which is the whole reason a
    caller cannot distinguish a slow start from a failed one.
    """
    if sys.platform == "win32":
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        for line in completed.stdout.splitlines():
            matched = _NETSTAT_ROW.match(line)
            if matched and int(matched.group(1)) == port:
                return int(matched.group(2))
        return None
    completed = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    first = completed.stdout.split()
    return int(first[0]) if first else None


def connectable(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start_server(home: Path, port: int, log: Path) -> "subprocess.Popen[bytes]":
    env = dict(os.environ)
    env["AGENTJOBS_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    handle = log.open("ab")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agentjobs.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=handle,
    )


def run_handover(home: Path, port: int) -> int:
    """Stop a server and start another, sampling the listening owner throughout."""
    log = home / "server.log"
    print(f"checkout {ROOT}")
    print(f"home     {home}")
    print(f"port     {port}")

    cold = time.monotonic()
    first = start_server(home, port, log)
    if not _wait_until_listening(port, first):
        return 1
    cold_seconds = time.monotonic() - cold
    print(f"first server listening after {cold_seconds:.1f}s, pid {listening_pid(port)}")

    transitions: List[Tuple[float, Optional[int]]] = []
    samples = 0
    started = time.monotonic()
    first.terminate()
    second: Optional["subprocess.Popen[bytes]"] = None
    alive = True
    try:
        while time.monotonic() - started < SERVER_START_TIMEOUT:
            samples += 1
            now = connectable(port)
            if now != alive:
                # The owner is asked for only when the answer can have changed. Asking
                # every sample means a `netstat` process ten times a second, and on this
                # machine that contends with the very start being timed -- a 54s cold
                # start became one that had not finished in 420s. Measured 2026-09-20.
                alive = now
                transitions.append(
                    (time.monotonic() - started, listening_pid(port) if now else None)
                )
            if second is None and not alive:
                # The predecessor has let go. Starting here is what `restart` does, and
                # starting earlier would only measure how long the old process took to
                # release the socket.
                second = start_server(home, port, log)
            elif second is not None and second.poll() is not None:
                print(f"the successor exited with code {second.returncode}", file=sys.stderr)
                return 1
            if alive and second is not None:
                break
            time.sleep(SAMPLE_INTERVAL)
        else:
            print(f"the successor never bound within {SERVER_START_TIMEOUT}s", file=sys.stderr)
            return 1
    finally:
        for process in (first, second):
            if process is not None:
                _stop(process)

    went_dark, back = transitions[0][0], transitions[-1][0]
    print()
    print(f"{'launch to listening, cold':32} {cold_seconds:>7.1f}s")
    print(f"{'connect probes':32} {samples:>8}")
    print(f"{'predecessor stopped listening':32} {went_dark:>7.1f}s")
    print(f"{'successor listening, pid':32} {transitions[-1][1]:>8}")
    print(f"{'DEAD PORT':32} {back - went_dark:>7.1f}s")
    print(f"{'stop to successor bound':32} {back:>7.1f}s")
    return 0


def _wait_until_listening(port: int, process: "subprocess.Popen[bytes]") -> bool:
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"server exited with code {process.returncode}", file=sys.stderr)
            return False
        if connectable(port):
            return True
        time.sleep(SAMPLE_INTERVAL)
    print(f"server did not listen within {SERVER_START_TIMEOUT}s", file=sys.stderr)
    return False


def _stop(process: "subprocess.Popen[bytes]") -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
        process.kill()
        process.wait(timeout=20)


# ---------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["phases", "handover"])
    parser.add_argument(
        "--home",
        type=Path,
        help="Measure against this AgentJobs home instead of seeding one. "
        "`phases` only -- it reads; `handover` would start a server against it.",
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS)
    parser.add_argument("--reaped", type=int, default=DEFAULT_REAPED)
    parser.add_argument(
        "--doomed",
        type=int,
        default=DEFAULT_DOOMED,
        help="Seeded runs the session manager has no job for. Before task-503 these "
        "were re-attempted on every start for ever.",
    )
    parser.add_argument("--port", type=int, default=checkout_port(ROOT))
    parser.add_argument("--keep", action="store_true", help="Leave the seeded home behind.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.home is not None:
        if args.mode == "handover":
            raise SystemExit("--home is for `phases`; `handover` starts a server, so it seeds.")
        return run_phases(args.home.expanduser().resolve(), seeded=False)

    home = Path(mkdtemp(prefix="agentjobs-startup-bench-"))
    seed_home(
        home,
        runs=args.runs,
        sessions=args.sessions,
        reaped=args.reaped,
        doomed=args.doomed,
    )
    print(
        f"seeded {args.runs} runs ({args.sessions} sessions, "
        f"{args.reaped} reaped, {args.doomed} doomed)"
    )
    try:
        if args.mode == "phases":
            return run_phases(home, seeded=True)
        return run_handover(home, args.port)
    finally:
        if args.keep:
            print(f"\nkept {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
