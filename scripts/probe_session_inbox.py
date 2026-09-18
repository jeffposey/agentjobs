"""Probe: can a plain Python process post into a live Claude Code session's inbox?

Task-449 spike. This is an investigation probe, not a production code path: nothing
in ``src/agentjobs`` imports it and nothing calls it but a person at a terminal.

What it does
------------
``roster`` prints every live Claude Code session this OS user can see, with the
inbox address and auth token AgentJobs would need to reach it.  ``send`` opens
that address, sends the documented auth line, then sends one candidate message
envelope and prints whatever comes back.

The auth line is documented -- ``{"type": "auth", "token": "<token>"}`` as the
first line of the connection, required on native Windows
(code.claude.com/docs/en/cross-session-messaging, "The session's inbox socket").
**The message envelope that follows it is not documented anywhere**, which is the
finding this probe exists to establish; ``--shape`` picks between guesses so the
guessing is visible rather than buried.

Usage
-----
    python scripts/probe_session_inbox.py roster
    python scripts/probe_session_inbox.py send --pid 12345 --text "hello" --shape a
    python scripts/probe_session_inbox.py send --self --text "hello" --shape a
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Candidate envelopes. The first line of the connection is always the documented
# auth line; these are guesses at the line after it.
SHAPES: dict[str, dict] = {
    "a": {"type": "message", "message": "<TEXT>"},
    "b": {"type": "message", "text": "<TEXT>"},
    "c": {"type": "message", "from": "probe", "message": "<TEXT>"},
    "d": {"type": "peer_message", "from": "probe", "message": "<TEXT>"},
    "e": {"type": "user_message", "message": {"role": "user", "content": "<TEXT>"}},
}


def _fill(template, text: str):
    if isinstance(template, dict):
        return {k: _fill(v, text) for k, v in template.items()}
    if template == "<TEXT>":
        return text
    return template


def read_roster() -> list[dict]:
    """Every live session's registration, plus its auth token where readable."""
    rows: list[dict] = []
    if not SESSIONS_DIR.is_dir():
        return rows
    for entry in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            row = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        row["_registration"] = str(entry)
        pid = row.get("pid")
        for key_file in SESSIONS_DIR.glob(f"{pid}.*.key"):
            try:
                row["_token"] = json.loads(key_file.read_text(encoding="utf-8")).get("peerToken")
                row["_key_file"] = str(key_file)
            except (OSError, ValueError):
                pass
            break
        rows.append(row)
    return rows


def cmd_roster(_args: argparse.Namespace) -> int:
    rows = read_roster()
    if not rows:
        print(f"no session registrations under {SESSIONS_DIR}")
        return 1
    for row in rows:
        print(
            json.dumps(
                {
                    "pid": row.get("pid"),
                    "kind": row.get("kind"),
                    "name": row.get("name"),
                    "status": row.get("status"),
                    "version": row.get("version"),
                    "sessionId": row.get("sessionId"),
                    "jobId": row.get("jobId"),
                    "cwd": row.get("cwd"),
                    "peerFeatures": row.get("peerFeatures"),
                    "messagingSocketPath": row.get("messagingSocketPath"),
                    "token_readable": bool(row.get("_token")),
                },
                indent=2,
            )
        )
    return 0


def _target(args: argparse.Namespace) -> tuple[str, str]:
    if args.self:
        path = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        token = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
        if not path or not token:
            raise SystemExit("this process has no CLAUDE_CODE_MESSAGING_* environment")
        return path, token
    for row in read_roster():
        if row.get("pid") == args.pid:
            path = row.get("messagingSocketPath")
            token = row.get("_token")
            if not path:
                raise SystemExit(f"pid {args.pid} registered no inbox address")
            if not token:
                raise SystemExit(f"pid {args.pid} has no readable auth token")
            return path, token
    raise SystemExit(f"pid {args.pid} is not in the roster")


def cmd_send(args: argparse.Namespace) -> int:
    path, token = _target(args)
    if args.bad_token:
        # Control: a wrong token must close the connection. Whether it does is how
        # we tell "auth was accepted and the envelope was wrong" from "auth never
        # worked", since a correct auth line draws no reply either way.
        token = "0" * len(token)
    envelope = _fill(SHAPES[args.shape], args.text)
    auth_line = json.dumps({"type": "auth", "token": token})
    body_line = json.dumps(envelope)

    print(f"address : {path}")
    print(f"auth    : {json.dumps({'type': 'auth', 'token': '<redacted>'})}")
    print(f"envelope: {body_line}")

    # Connect only once the payload is ready: the inbox closes a connection that
    # has not sent a complete line within 30 seconds.
    started = time.time()
    try:
        pipe = open(path, "r+b", buffering=0)
    except OSError as exc:
        print(f"RESULT  : could not open the address -- {exc!r}")
        return 1
    with pipe:
        try:
            pipe.write((auth_line + "\n").encode("utf-8"))
            pipe.write((body_line + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"RESULT  : write failed -- {exc!r}")
            return 1
        outcome: dict[str, bytes] = {}
        failure: dict[str, str] = {}

        def _reader() -> None:
            try:
                outcome["data"] = pipe.read(4096) or b""
            except OSError as exc:
                failure["error"] = repr(exc)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(args.wait)
        if thread.is_alive():
            # The peer neither answered nor hung up: it is holding the connection.
            print(f"reply   : (none in {args.wait:g}s; connection still open)")
            print("RESULT  : connection accepted and held -- auth line was not rejected")
            sys.stdout.flush()
            # Closing the handle blocks behind the pending read, so leave without it.
            os._exit(0)
        if failure:
            print(f"reply   : read failed -- {failure['error']}")
            return 1
        data = outcome.get("data") or b""
        if not data:
            print("reply   : (end of stream)")
            print("RESULT  : connection closed by the receiver -- auth line rejected")
            return 1
        print(f"reply   : {data.decode('utf-8', 'replace')!r}")
        print("RESULT  : receiver answered; see the line above")
    print(f"elapsed : {time.time() - started:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("roster", help="list live sessions, addresses and tokens").set_defaults(
        func=cmd_roster
    )

    send = sub.add_parser("send", help="post one candidate envelope into a session")
    send.add_argument("--pid", type=int, help="target session's process id")
    send.add_argument("--self", action="store_true", help="target this process's own session")
    send.add_argument("--text", default="probe", help="message text")
    send.add_argument("--shape", default="a", choices=sorted(SHAPES), help="envelope guess")
    send.add_argument("--wait", type=float, default=3.0, help="seconds to read a reply")
    send.add_argument(
        "--bad-token",
        action="store_true",
        help="control run: send a deliberately wrong auth token",
    )
    send.set_defaults(func=cmd_send)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
