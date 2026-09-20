"""Time one realistic drafting round trip down each path AgentJobs could take to a model.

Task-174 asks which of three paths AgentJobs may use to call a model for a short,
human-triggered draft, and requires the latency to be *measured* rather than estimated.
This script is that measurement, kept in the repository so the number can be re-taken on
another machine or after the paths change rather than quoted from a task record forever.

It measures what this machine actually has and says so when a path is absent; an
unconfigured path is not a failure here, it is the evidence for task-174's
degrade-gracefully requirement.

    poetry run python scripts/model_access_probe.py             # every available path
    poetry run python scripts/model_access_probe.py --path cli  # just one
    poetry run python scripts/model_access_probe.py --repeat 3

**This spends real money or real plan quota.** It is deliberately not a gate stage and
nothing imports it; it runs when a person runs it.

The prompt is a realistic one for task-175 -- a one-line task description expanded into a
full spec -- because the number that matters is the one a person waits through, and a
toy prompt would measure the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

ONE_LINER = "the task list gets slow once a project has a few hundred tasks, fix the paging"

PROMPT = f"""You are drafting an AgentJobs task record. Expand the one-liner below into a
task spec that satisfies the Resumption Contract: a summary that orients a reader with no
other context, an intent saying why it matters, a description that is a working
specification, and acceptance criteria that are independently verifiable.

Return ONLY a JSON object with keys: summary, intent, description, constraints,
out_of_scope, acceptance (a list of strings). No prose outside the JSON.

Do not set lifecycle, ball, priority, parent, dependencies or actor.

One-liner from the human:
"{ONE_LINER}"
"""

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OLLAMA_GENERATE_URL = "http://127.0.0.1:11434/api/generate"


@dataclass
class Measurement:
    """One timed attempt down one path."""

    path: str
    detail: str
    seconds: Optional[float]
    chars: int
    parsed_json: bool
    unavailable: Optional[str] = None

    def line(self) -> str:
        if self.unavailable is not None:
            return f"{self.path:<10} unavailable -- {self.unavailable}"
        shape = "json" if self.parsed_json else "NOT json"
        assert self.seconds is not None
        return (
            f"{self.path:<10} {self.seconds:7.2f}s  {self.chars:>6} chars  {shape:<8} {self.detail}"
        )


def _looks_like_json(text: str) -> bool:
    """Whether the reply is the JSON object a form could actually fill fields from.

    A fenced block counts: stripping a fence is the caller's problem, not the model's
    failure. Prose wrapped around the object does not, because that is the case where a
    caller has to guess which part of the answer was the answer.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
    try:
        return isinstance(json.loads(stripped.strip()), dict)
    except (ValueError, IndexError):
        return False


def measure_cli(model: str) -> Measurement:
    """Option A: spawn the agent CLI a human already configured, in one-shot print mode.

    Measured in its *fairest* form, which is not the form a dispatch runner is configured
    in. ``--strict-mcp-config`` with no servers, an empty ``--allowed-tools`` and an empty
    ``--setting-sources`` is the closest this path gets to a completion endpoint: no MCP
    servers, no tools, and none of the operator's own ``CLAUDE.md``. It is still a whole
    agent process starting up, which is the cost being measured.

    The last of those three flags is load-bearing and is why this function names them
    rather than reusing a configured runner's argv. Without ``--setting-sources ''`` the
    call loads the operator's global standing instructions -- verified on 2026-09-19 by
    asking a one-shot invocation what it had been given, which named the operator by name
    and listed private repository paths. Those flags are specific to this CLI; a runner
    argv in ``dispatch.yaml`` is an opaque human-written list, so reusing one would
    measure something no caller could safely reproduce.
    """
    binary = shutil.which("claude")
    if binary is None:
        return Measurement("cli", model, None, 0, False, unavailable="no `claude` on PATH")

    argv = [
        binary,
        "-p",
        "--model",
        model,
        "--output-format",
        "text",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--allowed-tools",
        "",
        "--setting-sources",
        "",
    ]
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        input=PROMPT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        head = (proc.stderr or "").strip().splitlines()
        return Measurement(
            "cli", model, None, 0, False, unavailable=f"exit {proc.returncode}: {head[:1]}"
        )
    out = proc.stdout or ""
    return Measurement("cli", model, elapsed, len(out), _looks_like_json(out))


def measure_api_transport_floor() -> Optional[float]:
    """Time everything option B pays for *except* the inference, with no credential.

    An unauthenticated request is refused by the endpoint, so what this times is DNS, the
    TLS handshake and one HTTP round trip -- the whole of option B's overhead above the
    model call itself. It exists because a machine with no key can still establish the
    floor honestly, rather than leaving the comparison against option A's measured
    process-startup cost to an assertion.

    Returns ``None`` when the network is unreachable; that is a missing measurement, not
    a fast one.
    """
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=b"{}",
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01"},
    )
    started = time.monotonic()
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError:
        # The expected path: the endpoint answered, refusing the missing credential.
        return time.monotonic() - started
    except (urllib.error.URLError, TimeoutError):
        return None
    return time.monotonic() - started


def measure_api(model: str) -> Measurement:
    """Option B: one HTTPS request to the Messages API, with a key from the environment.

    Deliberately ``urllib`` rather than the vendor SDK: the point is to time the network
    round trip without also committing this repository to a dependency the decision has
    not yet been taken on.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        floor = measure_api_transport_floor()
        detail = "ANTHROPIC_API_KEY not set"
        if floor is not None:
            detail += f"; transport floor {floor:.2f}s (TLS + HTTP round trip, no inference)"
        return Measurement("api", model, None, 0, False, unavailable=detail)

    body = json.dumps(
        {
            "model": model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": key,
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Never echo the request: the header carrying the key is in it.
        return Measurement("api", model, None, 0, False, unavailable=f"{type(exc).__name__}")
    elapsed = time.monotonic() - started
    text = "".join(
        block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
    )
    return Measurement("api", model, elapsed, len(text), _looks_like_json(text))


def measure_ollama(model: str) -> Measurement:
    """Option C: a local model over Ollama's HTTP API."""
    body = json.dumps({"model": model, "prompt": PROMPT, "stream": False}).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_GENERATE_URL, data=body, headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return Measurement(
            "ollama",
            model,
            None,
            0,
            False,
            unavailable=f"{type(exc).__name__} at {OLLAMA_GENERATE_URL}",
        )
    elapsed = time.monotonic() - started
    text = str(payload.get("response", ""))
    return Measurement("ollama", model, elapsed, len(text), _looks_like_json(text))


PATHS: Dict[str, Callable[[str], Measurement]] = {
    "cli": measure_cli,
    "api": measure_api,
    "ollama": measure_ollama,
}

DEFAULT_MODELS: Dict[str, str] = {
    "cli": "claude-haiku-4-5-20251001",
    "api": "claude-haiku-4-5-20251001",
    "ollama": "llama3.1",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--path", action="append", choices=sorted(PATHS), help="Repeatable; default is every path."
    )
    parser.add_argument("--model", help="Override the model for every selected path.")
    parser.add_argument("--repeat", type=int, default=1, help="Attempts per path (default 1).")
    args = parser.parse_args(argv)

    chosen = args.path or sorted(PATHS)
    results: List[Measurement] = []
    for name in chosen:
        model = args.model or DEFAULT_MODELS[name]
        for _ in range(max(1, args.repeat)):
            result = PATHS[name](model)
            print(result.line(), flush=True)
            results.append(result)
            if result.unavailable is not None:
                break

    timed = [r for r in results if r.seconds is not None]
    if timed:
        print()
        print(f"{len(timed)} timed attempt(s); slowest {max(r.seconds or 0 for r in timed):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
