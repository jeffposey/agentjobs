"""The replay harness's fake Claude CLI, answerable two ways (task-525).

It answers the commands dispatch sends a runner -- ``agents``, ``logs``, ``stop``, ``-p``,
``--bg --resume`` and a launch -- with rows shaped like Claude Code 2.1.270's, from files
in the directory it lives in: ``ledger.json`` is the session listing, ``store.json`` says
whether the credential store answers a probe, and ``*.log`` record what was asked.

**As a script**, copied beside those files as ``claude.py`` and run by a real interpreter.
That is how a crash child reaches it: a child is a separate process on purpose, and what
it spawns is real.

**In-process**, through :data:`agentjobs.dispatch.program.INSTALLED`, which is how the
harness's own process reaches it. The scenarios are about what the controller, recovery
and the finisher *decide* from an answer, not about a process printing one, and task-518
measured three of them spawning 120 interpreters between them to get those answers.

One function answers both, so the two paths cannot drift apart. It reads stdin only when
asked, through a callable, because a launch inherits stdin and must never block on it.
"""

import json
import pathlib
import sys
from typing import Callable, List, Sequence, Tuple


def answer(
    argv: Sequence[str], read_stdin: Callable[[], str], cwd: pathlib.Path, here: pathlib.Path
) -> Tuple[int, str, str]:
    """``(exit code, stdout, stderr)`` for one invocation of the fake CLI."""
    argv = list(argv)
    ledger = here / "ledger.json"
    out: List[str] = []

    def say(text: str) -> None:
        out.append(text + "\n")

    def rows() -> list:
        return json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else []

    def save(value: list) -> None:
        ledger.write_text(json.dumps(value), encoding="utf-8")

    def log(name: str, value: object) -> None:
        with (here / name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value) + "\n")

    def done(code: int) -> Tuple[int, str, str]:
        return code, "".join(out), ""

    if argv[:1] == ["agents"]:
        listed = rows() if "--all" in argv else [r for r in rows() if r.get("state") != "stopped"]
        say(json.dumps(listed))
        return done(0)

    if argv[:1] == ["logs"]:
        say("session output")
        return done(0)

    if argv[:1] == ["stop"]:
        log("calls.log", argv)
        updated = []
        for row in rows():
            if row["id"] == argv[1]:
                row.update({"pid": None, "status": "stopped", "state": "stopped"})
            updated.append(row)
        save(updated)
        say("stopped")
        return done(0)

    if argv[:1] == ["-p"]:
        read_stdin()
        store = json.loads((here / "store.json").read_text(encoding="utf-8"))
        log("probes.log", {"argv": argv, "store": store})
        if store["answer"] == "ready":
            say(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "api_error_status": None,
                        "result": "AUTH_OK",
                        "num_turns": 1,
                    }
                )
            )
            return done(0)
        say(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "api_error_status": store.get("status", 401),
                    "result": store["text"],
                }
            )
        )
        return done(store.get("exit", 1))

    if argv[:2] == ["--bg", "--resume"]:
        message = read_stdin()
        log("nudges.log", {"argv": argv, "stdin": message})
        updated = []
        for row in rows():
            if row.get("sessionId") == argv[2]:
                row.update({"pid": 5151, "status": "busy", "state": "working"})
            updated.append(row)
        save(updated)
        reply = here / "reply_on_wake.json"
        if reply.is_file():
            plan = json.loads(reply.read_text(encoding="utf-8"))
            target = plan["transcripts"].get(argv[2])
            if target:
                with open(target, "a", encoding="utf-8") as handle:
                    for line in plan["lines"][argv[2]]:
                        handle.write(json.dumps(line) + "\n")
        say("woke session " + argv[2][:8] + " with its saved options (--model)")
        return done(0)

    current = rows()
    number = len(current)
    short = "%08x" % (0xA19E0000 + number)
    full = short + "-0000-4000-8000-%012d" % number
    name = argv[argv.index("--name") + 1] if "--name" in argv else ""
    model = argv[argv.index("--model") + 1] if "--model" in argv else ""
    log("launches.log", {"id": short, "name": name, "model": model})
    current.append(
        {
            "id": short,
            "sessionId": full,
            "cwd": str(cwd),
            "kind": "background",
            "name": name,
            "pid": 4000 + number,
            "startedAt": 1787087345053,
            "status": "busy",
            "state": "working",
        }
    )
    save(current)
    say("backgrounded · " + short + " · " + name)
    return done(0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    code, stdout, stderr = answer(
        sys.argv[1:], sys.stdin.read, pathlib.Path.cwd(), pathlib.Path(__file__).parent
    )
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    raise SystemExit(code)
