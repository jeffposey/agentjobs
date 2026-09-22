"""Prove a test still catches the threshold it was written to cover (task-518).

**The trap this exists to spring.** Task-518 made a set of timing tests fast by giving the
dispatch subsystem a clock a test owns, so a ninety-second window is waited out in no time
at all. The obvious way to get the same speed is to shorten the window, and the difference
between the two is invisible in a diff: both give you a green test in a second. One of them
still fails when the rule it covers is removed from the product; the other does not.

So the check is not "is this test still green". It is: **take the production threshold
away, and does this test go red?** A test that stays green without the rule was not
testing the rule.

    poetry run python scripts/threshold_probe.py            # every case
    poetry run python scripts/threshold_probe.py --case auth-probe-period
    poetry run python scripts/threshold_probe.py --list

Each case names a constant in the product, a value that removes what it expresses, and the
tests that should notice. The probe runs those tests twice: once untouched, expecting
green, and once with the constant replaced, expecting red. Both halves matter -- a case
whose "before" arm is red is not evidence about anything, and the probe says so rather
than reporting a pass.

**It patches nothing on disk.** The replacement is applied by a pytest plugin (this same
file, loaded with ``-p``) at the start of the session, from an environment variable. A
probe that edited the source and restored it afterwards would leave the product broken in
the working tree if it were interrupted, which on a shared clone is somebody else's
afternoon.

**A test with no threshold is not a failing case**, it is a different kind of test, and
:data:`NOT_COVERED` below names the ones task-518 touched that are in that class, with the
evidence for each. Two of them are there because a case was written for them and the probe
reported MISSED -- which is the tool working.

Not a gate stage: it runs each case's tests twice, one case is expected to reach its
timeout, and it is a check on the tests rather than on the code. Run it when converting a
timing test, and when a threshold changes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
BREAK_ENV = "AGENTJOBS_THRESHOLD_BREAK"


@dataclass(frozen=True)
class Case:
    """One threshold, the value that removes it, and who should notice."""

    name: str
    target: str
    """Dotted path to the constant, e.g. ``agentjobs.dispatch.standdown:CONFIRM``."""
    broken: object
    """What the constant becomes. Not a smaller number -- a value that removes the rule."""
    why: str
    """What the threshold means, and therefore what its absence should break."""
    tests: List[str] = field(default_factory=list)
    hangs: bool = False
    """True when removing this threshold does not fail the test but stops it returning.

    A bound on a wait is the case: with no bound the code waits for ever, so the broken
    arm neither passes nor fails -- it hangs, and the probe has to cut it off. That is
    still the threshold being caught, and it is worth stating separately rather than
    dressing up as a red, because a reader should know which cases cost a timeout.
    """


CASES: List[Case] = [
    Case(
        name="standdown-window-unbounded",
        target="agentjobs.dispatch.standdown:STAND_DOWN_CONFIRM_SECONDS",
        broken=1_000_000.0,
        why=(
            "The same window from the other side: it is bounded, so a session that will "
            "not stop is eventually left alone with its lock and nothing is merged. "
            "Unbounded, the finish waits for ever instead of declining -- so this one is "
            "caught by not returning rather than by going red."
        ),
        hangs=True,
        tests=[
            "tests/test_approval_standdown.py::TestApprovingWhileTheSessionIsStillAttached"
            "::test_a_session_that_will_not_stop_merges_nothing_until_the_poller_sees_it_end",
        ],
    ),
    Case(
        name="auth-probe-period",
        target="agentjobs.dispatch.auth_recovery:POLICIES",
        broken={"__probe_period__": 1_000_000.0},
        why=(
            "A stalled session's store is re-probed every sixty seconds until it answers. "
            "Stretch the period past the test's timeline and the store's recovery is never "
            "noticed, so the run is never resumed."
        ),
        tests=[
            "tests/test_auth_recovery.py::TestSelfHealingNeedsNobody"
            "::test_a_store_that_recovers_is_probed_and_the_session_resumed_in_place",
        ],
    ),
    Case(
        name="auth-notify-after",
        target="agentjobs.dispatch.auth_recovery:POLICIES",
        broken={"__notify_after__": 0.0},
        why=(
            "Nobody is paged for a blip: a person hears about an auth stall only after "
            "five minutes of it. With no delay the first tick pages them, which is the "
            "behaviour task-224 was filed to remove."
        ),
        tests=[
            "tests/test_auth_recovery.py::TestSelfHealingNeedsNobody"
            "::test_a_store_that_recovers_is_probed_and_the_session_resumed_in_place",
        ],
    ),
    Case(
        name="auth-nudge-lease",
        target="agentjobs.dispatch.auth_recovery:NUDGE_LEASE_SECONDS",
        broken=1_000_000.0,
        why=(
            "A resume whose result was lost is reconciled once the lease expires. An "
            "unexpiring lease leaves the waiter stuck at `nudging` for ever, so a crash "
            "mid-resume is never recovered from."
        ),
        tests=[
            "tests/test_auth_recovery.py::TestALostAcknowledgement"
            "::test_a_delivered_message_is_recognised_and_not_sent_again",
        ],
    ),
    Case(
        name="auth-confirm-seconds",
        target="agentjobs.dispatch.auth_recovery:CONFIRM_SECONDS",
        broken=0.0,
        why=(
            "A resumed session gets fifteen minutes to say something before the recovery "
            "gives up and escalates to a person. With no window the first tick after the "
            "resume escalates, so a session that was about to answer is handed to a human."
        ),
        tests=[
            "tests/test_auth_recovery.py::TestALostAcknowledgement"
            "::test_a_delivered_message_is_recognised_and_not_sent_again",
        ],
    ),
]

NOT_COVERED = """Tests task-518 made fast that have no threshold to remove, and why that
is the right answer for them rather than a gap:

-   `test_cli.py::test_show_task_not_found`, and the two `test_mcp_server.py` startup
    probes. Each asserts an *absence* -- a task that is not there, a service that is not
    running -- and was slow because it waited out a retry budget meant for riding through
    a restart. There was never a wait they were asserting on, so there is nothing to take
    away; they are 30s, 32s and 23s faster and their assertions are unchanged.

-   `test_approval_standdown.py::...::test_a_busy_session_stands_down_and_the_approval_merges`.
    Probed with the stand-down window set to zero, **and it stayed green** -- the session
    it stands down is observed stopped on the first poll, so the window is never consulted.
    The test covers the transfer, not the window; `...::test_a_session_that_will_not_stop...`
    covers the window, and does catch its removal. Recorded rather than quietly retargeted,
    because "this test does not cover what its neighbours cover" is the finding.

-   `test_client_retry.py`. Its own module docstring says it monkeypatches the backoff to
    zero and asserts on the *number of attempts* rather than elapsed time, precisely so it
    means the same thing on every machine. So it covers the retry loop and not the
    constant, and a probe that replaced the constant is answered by the fixture before the
    test runs. Verified by running it: the case reported MISSED for that reason and was
    removed rather than made to pass.
"""


# ----- the plugin half --------------------------------------------------------------


def pytest_configure(config: object) -> None:
    """Apply the requested replacement before anything imports a module that reads it.

    Named ``pytest_configure`` because this file is also loaded as a plugin with ``-p``;
    running the probe and being the thing it installs are the same file so that the value
    and the constant it replaces cannot drift apart in two places.
    """
    del config
    raw = os.environ.get(BREAK_ENV)
    if not raw:
        return
    request = json.loads(raw)
    module_name, _, attribute = request["target"].partition(":")
    import importlib

    module = importlib.import_module(module_name)
    current = getattr(module, attribute)
    replacement = request["broken"]
    if isinstance(replacement, dict) and set(replacement) <= {
        "__probe_period__",
        "__notify_after__",
    }:
        # `POLICIES` is a mapping of kind to a frozen policy; the threshold inside it is
        # what a case means, so the field is replaced on every kind rather than the map.
        import dataclasses

        field_name = "probe_period" if "__probe_period__" in replacement else "notify_after"
        value = list(replacement.values())[0]
        setattr(
            module,
            attribute,
            {
                kind: dataclasses.replace(policy, **{field_name: value})
                for kind, policy in current.items()
            },
        )
        return
    if isinstance(replacement, list):
        replacement = tuple(replacement)
    setattr(module, attribute, replacement)


# ----- the runner half --------------------------------------------------------------


TIMED_OUT = -9999


def run(case: Case, *, broken: bool, timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests"), env.get("PYTHONPATH", "")]
    )
    env.pop(BREAK_ENV, None)
    if broken:
        env[BREAK_ENV] = json.dumps({"target": case.target, "broken": case.broken})
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *case.tests,
        "-p",
        "threshold_probe",
        "-p",
        "no:randomly",
        "-q",
        "--no-header",
        "-x",
    ]
    try:
        return subprocess.run(
            argv, cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as expired:
        return subprocess.CompletedProcess(
            argv,
            TIMED_OUT,
            (expired.stdout or b"").decode("utf-8", "replace")
            if isinstance(expired.stdout, bytes)
            else (expired.stdout or ""),
            f"timed out after {timeout:.0f}s",
        )


def probe(case: Case, *, timeout: float) -> Dict[str, object]:
    started = time.monotonic()
    intact = run(case, broken=False, timeout=timeout)
    without = run(case, broken=True, timeout=timeout)
    if intact.returncode != 0:
        verdict = "BROKEN BEFORE"
    elif without.returncode == TIMED_OUT:
        verdict = "caught (hung)" if case.hangs else "HUNG, NOT EXPECTED TO"
    elif without.returncode != 0:
        verdict = "MISSED (hang expected)" if case.hangs else "caught"
    else:
        verdict = "MISSED"
    return {
        "case": case.name,
        "verdict": verdict,
        "intact": intact.returncode,
        "without": without.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "tail": (without.stdout or "").strip().splitlines()[-4:],
    }


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", help="run only these cases")
    parser.add_argument("--list", action="store_true", help="name the cases and stop")
    parser.add_argument(
        "--timeout",
        type=float,
        default=150.0,
        help="seconds before a run is cut off; a case marked `hangs` is expected to reach it",
    )
    args = parser.parse_args(argv)

    if args.list:
        for case in CASES:
            print(f"{case.name:28} {case.target}")
            print(f"{'':28} {case.why}")
        return 0

    chosen = [c for c in CASES if not args.case or c.name in args.case]
    if not chosen:
        print(f"No such case. Known: {', '.join(c.name for c in CASES)}")
        return 2

    results = [probe(case, timeout=args.timeout) for case in chosen]
    print()
    print(f"{'case':28} {'verdict':16} {'intact':>7} {'without':>8} {'secs':>6}")
    for row in results:
        print(
            f"{row['case']:28} {row['verdict']:16} {row['intact']:>7} "
            f"{row['without']:>8} {row['seconds']:>6}"
        )
    missed = [row for row in results if not str(row["verdict"]).startswith("caught")]
    for row in missed:
        print(f"\n--- {row['case']} ({row['verdict']}) ---")
        for line in row["tail"]:
            print(f"    {line}")
    if missed:
        print(
            f"\n{len(missed)} case(s) did not catch the missing threshold. A test that "
            "stays green without the rule is not testing the rule."
        )
        return 1
    print(f"\nAll {len(results)} thresholds are covered: each goes red when it is removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
