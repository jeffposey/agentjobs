"""Drive the scripted finish (task-241) against a real clone of this repository.

The mechanism's unit tests build small synthetic repositories, which is the right shape
for asserting on behaviour and the wrong shape for answering "does this work". The four
things this feature is judged on are all statements about the real world:

* a clean, green branch merges, closes, restarts a real server and is *seen* to be live;
* a conflicting rebase is aborted and the branch survives byte for byte;
* a red gate blocks the merge;
* a server that did not come back on the merged code escalates rather than reporting a
  finish.

So this clones this repository, bootstraps it, starts a real AgentJobs server out of the
clone, and runs the real ``agentjobs finish`` against real branches in it. Nothing is
stubbed and nothing outside the sandbox directory is touched: its own clone, its own
``AGENTJOBS_HOME``, its own port derived from its own path.

    python scripts/finish_sandbox.py                 # every case
    python scripts/finish_sandbox.py --case happy    # one of them
    python scripts/finish_sandbox.py --keep          # leave the sandbox to poke at

Budget about eight minutes for the full set: most of it is one real gate run on the
happy-path branch, which is the point rather than an overhead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
ACTIVATION_VARS = ("VIRTUAL_ENV", "POETRY_ACTIVE")

for _stream in (sys.stdout, sys.stderr):
    # The CLI this drives prints a tick on success, and a redirected stdout on Windows is
    # cp1252, which cannot encode one. So the run that *succeeded* was the run that died
    # -- with a UnicodeEncodeError, three minutes in, after a real merge. Observed.
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CASES = ("happy", "conflict", "red-gate", "stale-server")


def env() -> Dict[str, str]:
    """This process's environment with any activated virtualenv removed.

    The same scrub the finish itself does, and for the same reason: this script is
    normally run from a worktree whose shell has the main clone's environment activated,
    and Poetry prefers an activated environment over the one it keys on a project path.
    """
    return {key: value for key, value in os.environ.items() if key not in ACTIVATION_VARS}


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    extra_env: Optional[Dict[str, str]] = None,
    quiet: bool = False,
) -> "subprocess.CompletedProcess[str]":
    merged = env()
    if extra_env:
        merged.update(extra_env)
    if not quiet:
        print(f"  $ {' '.join(str(word) for word in argv)}")
    result = subprocess.run(
        [str(word) for word in argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
    )
    if check and result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"failed ({result.returncode}): {' '.join(str(w) for w in argv)}")
    return result


def git(root: Path, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    return run(["git", "-C", str(root), *args], cwd=root, check=check, quiet=True)


def head(root: Path, ref: str = "HEAD") -> str:
    return git(root, "rev-parse", ref).stdout.strip()


def free_port(seed: str) -> int:
    """A port this sandbox owns, derived from its own path then confirmed free.

    Derived rather than fixed because several checkouts of this repository run their own
    servers and gates at once; confirmed rather than only derived because a derived port
    that happens to be taken fails in a way nobody would connect to the derivation.
    """
    digest = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6], 16)
    candidate = 30000 + digest % 20000
    for offset in range(50):
        port = candidate + offset
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("no free port near the derived one")


# ----- building the sandbox ---------------------------------------------------


def remove_tree(path: Path) -> None:
    """Delete a sandbox, including the read-only files git leaves in its object store.

    ``ignore_errors=True`` on its own is worse than useless here: on Windows a git
    object is read-only, so the removal silently leaves a partial tree behind and the
    next run fails at `git clone` with "destination path already exists". Observed on
    the second run of this script.
    """
    import stat

    def force(func: object, target: str, _exc: object) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            os.remove(target)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=force)


class Sandbox:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.clone = base / "clone"
        self.home = base / "home"
        self.worktrees = base / "worktrees"
        self.port = free_port(str(base))
        self._python: Optional[Path] = None

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def python(self) -> Path:
        """The clone's own interpreter, asked of Poetry once and remembered.

        Named rather than reached through ``poetry run`` for the reason ALLAGENTS.md
        gives: ``poetry run`` prefers whatever virtualenv the calling shell activated,
        which in a worktree is the wrong checkout's.
        """
        if self._python is None:
            suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
            venv = run(["poetry", "env", "info", "--path"], cwd=self.clone, quiet=True)
            self._python = Path(venv.stdout.strip()) / suffix
        return self._python

    def build(self, branch: str) -> None:
        print(f"\n== building the sandbox in {self.base}")
        self.base.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.worktrees.mkdir(parents=True, exist_ok=True)

        # A real clone of this repository, with the branch under test as its `main`, so
        # the finish being exercised is the code being reviewed rather than whatever is
        # currently merged.
        run(["git", "clone", "--quiet", str(ROOT), str(self.clone)], cwd=self.base)
        git(self.clone, "checkout", "-q", "-B", "main", f"origin/{branch}")
        git(self.clone, "config", "user.email", "sandbox@example.invalid")
        git(self.clone, "config", "user.name", "Finish sandbox")

        print("  bootstrapping the clone (poetry install, npm ci) -- about 30s")
        run([sys.executable, "scripts/bootstrap.py"], cwd=self.clone)

        run(
            [
                str(self.python),
                "-m",
                "agentjobs.cli",
                "project",
                "add",
                str(self.clone),
                "--id",
                "sandbox",
                "--name",
                "Finish sandbox",
            ],
            cwd=self.clone,
            extra_env=self.agent_env(),
        )
        self.write_dispatch_config(restart=True)

    def agent_env(self) -> Dict[str, str]:
        return {"AGENTJOBS_HOME": str(self.home)}

    def write_dispatch_config(self, *, restart: bool) -> None:
        """The machine-local config the finish reads. Written per case.

        ``restart: false`` writes a command that succeeds and restarts nothing, which is
        precisely what a wrong restart command looks like from the outside: exit zero,
        and the same process still answering on the port.
        """
        restart_argv = (
            [str(self.python), str(self.base / "restart.py")]
            if restart
            else [str(self.python), "-c", "pass"]
        )
        config = {
            "version": 1,
            "enabled": True,
            "api_base": self.api_base,
            "runners": {"noop": {"argv": ["python", "-c", "pass", "{prompt}"], "mode": "batch"}},
            "projects": {
                "sandbox": {
                    "enabled": True,
                    "runner": "noop",
                    "auto_dispatch": False,
                    "finish": {
                        "enabled": True,
                        "base_branch": "main",
                        "restart": restart_argv,
                        "verify_base": self.api_base,
                        "verify_timeout_seconds": 60,
                    },
                }
            },
            "limits": {"max_concurrent_runs": 1},
        }
        import yaml

        (self.home / "dispatch.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )

    def write_restart_script(self) -> None:
        """A restart command shaped like a real one: stop what is there, start it again.

        Deliberately not ``agentjobs restart``. This sandbox does not serve on the
        default port, and the whole reason ``finish.restart`` is configuration is that
        the CLI's restart binds 8765 and reports success while the real server stays
        exactly where it was.
        """
        script = f'''"""Restart the sandbox server, the way the sandbox started it."""
import os, subprocess, sys, time, urllib.request
from pathlib import Path

BASE = Path(r"{self.base}")
PID = BASE / "server.pid"
LOG = BASE / "server.log"

if PID.is_file():
    try:
        pid = int(PID.read_text().strip())
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except (ValueError, OSError, ProcessLookupError):
        pass
    time.sleep(1.5)

handle = LOG.open("a", encoding="utf-8")
process = subprocess.Popen(
    [r"{self.python}", "-m", "agentjobs.cli", "serve", "--port", "{self.port}"],
    cwd=r"{self.clone}",
    env={{**os.environ, "AGENTJOBS_HOME": r"{self.home}"}},
    stdout=handle,
    stderr=subprocess.STDOUT,
)
PID.write_text(str(process.pid))

deadline = time.time() + 60
while time.time() < deadline:
    try:
        with urllib.request.urlopen("{self.api_base}/api/health", timeout=2):
            print("server is up on {self.api_base}")
            raise SystemExit(0)
    except Exception:
        time.sleep(0.5)
raise SystemExit("the sandbox server did not come up")
'''
        (self.base / "restart.py").write_text(script, encoding="utf-8")

    def start_server(self) -> None:
        self.write_restart_script()
        print("  starting the sandbox server")
        run([str(self.python), str(self.base / "restart.py")], cwd=self.base)
        print(f"  {self.api_base} -> {json.dumps(self.version())}")

    def version(self) -> Dict[str, object]:
        try:
            with urllib.request.urlopen(f"{self.api_base}/api/version", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def stop_server(self) -> None:
        pid_file = self.base / "server.pid"
        if not pid_file.is_file():
            return
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.kill(pid, 15)
            except (OSError, ProcessLookupError):
                pass

    # ----- tasks and branches -------------------------------------------------

    def create_task(self, title: str, branch: str) -> str:
        payload = {
            "title": title,
            "description": "A sandbox task standing in for real work on a branch.",
            "category": "infrastructure",
            "lifecycle": "ready",
        }
        request = urllib.request.Request(
            f"{self.api_base}/api/tasks",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            task_id = json.loads(response.read().decode("utf-8"))["id"]

        self.post(f"/api/tasks/{task_id}/claim", {"agent": "claude"})
        self.patch(
            f"/api/tasks/{task_id}",
            {"branches": [{"name": branch, "status": "active"}]},
        )
        return str(task_id)

    def post(self, path: str, payload: Dict[str, object]) -> Dict[str, object]:
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return dict(json.loads(response.read().decode("utf-8")))

    def patch(self, path: str, payload: Dict[str, object]) -> Dict[str, object]:
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return dict(json.loads(response.read().decode("utf-8")))

    def task(self, task_id: str) -> Dict[str, object]:
        with urllib.request.urlopen(f"{self.api_base}/api/tasks/{task_id}", timeout=15) as r:
            return dict(json.loads(r.read().decode("utf-8")))

    def branch_worktree(self, branch: str, name: str) -> Path:
        """A worktree for a branch, bootstrapped, exactly as a real agent's would be.

        Bootstrapped even for the cases that never reach the gate. Preflight refuses a
        worktree whose interpreter Poetry cannot name -- correctly, since a branch that
        cannot be gated cannot be merged -- so a sandbox that skipped this would stop at
        ``no_interpreter`` every time and demonstrate nothing about conflicts or red
        gates. Found by running it: the first pass of this script did exactly that.
        """
        path = self.worktrees / name
        git(self.clone, "worktree", "add", "-b", branch, str(path), "main")
        print(f"  bootstrapping {path} so its gate can run -- about 20s")
        run([sys.executable, "scripts/bootstrap.py"], cwd=path, quiet=True)
        return path

    def finish(self, task_id: str) -> "subprocess.CompletedProcess[str]":
        started = time.monotonic()
        result = run(
            [
                str(self.python),
                "-m",
                "agentjobs.cli",
                "finish",
                task_id,
                "--project",
                "sandbox",
                "--approver",
                "Jeff Posey",
            ],
            cwd=self.clone,
            extra_env=self.agent_env(),
            check=False,
        )
        print(result.stdout)
        print(f"  ({time.monotonic() - started:.1f}s, exit {result.returncode})")
        return result


# ----- the cases --------------------------------------------------------------


def outcome_of(result: "subprocess.CompletedProcess[str]") -> str:
    """The `<task>: <outcome> (<reason>)` line the finish prints, as a reason string.

    Asserted on rather than only checking the exit code, because "it stopped" is not the
    claim any of these cases is making -- each one names the specific thing it expects to
    have stopped it, and a case that stopped for a different reason has demonstrated
    nothing. The first pass of this script checked only the shape, and reported a
    `no_interpreter` refusal as a successful demonstration of a rebase conflict.
    """
    match = re.search(r"^\S+: \w+ \((\w+)\)", result.stdout or "", re.MULTILINE)
    return match.group(1) if match else "?"


def case_conflict(box: Sandbox) -> bool:
    """f3: a conflicting rebase is aborted and the branch is left byte-identical."""
    print("\n== case: a rebase that does not apply cleanly")
    branch = "sandbox/conflict"
    # A scratch file, committed to main before the branch is cut, rather than an
    # existing one. The first version of this rewrote README.md on both sides, which
    # conflicts perfectly and also breaks `test_documentation_contract` for every case
    # that runs afterwards -- so the *next* case's gate went red for a reason belonging
    # to this one, and reported it as its own finding. Observed, and worth stating:
    # a sandbox that damages shared state demonstrates the wrong thing downstream.
    scratch = "sandbox-conflict.txt"
    (box.clone / scratch).write_text("the version both sides will rewrite\n", encoding="utf-8")
    git(box.clone, "add", "--", scratch)
    git(box.clone, "commit", "-qm", "a scratch file for the conflict case")

    worktree = box.branch_worktree(branch, "conflict")
    (worktree / scratch).write_text("the branch's version\n", encoding="utf-8")
    git(worktree, "commit", "-qam", "the branch rewrites the scratch file")
    (box.clone / scratch).write_text("main's version\n", encoding="utf-8")
    git(box.clone, "commit", "-qam", "main rewrites the scratch file")

    task_id = box.create_task("Conflicting branch", branch)
    before = head(box.clone, branch)
    main_before = head(box.clone, "main")

    result = box.finish(task_id)

    after = head(box.clone, branch)
    task = box.task(task_id)
    merged = git(box.clone, "merge-base", "--is-ancestor", branch, "main", check=False)
    reason = outcome_of(result)
    ok = (
        result.returncode == 1
        and reason == "rebase_conflict"
        and after == before
        and merged.returncode != 0
        and task["lifecycle"] == "active"
        and "Nothing was merged" in str(task.get("ball_prompt") or "")
    )
    print(f"  stopped at: {reason}")
    print(f"  branch {before[:8]} -> {after[:8]} ({'unchanged' if after == before else 'MOVED'})")
    print(f"  main was {main_before[:8]}, branch merged into main: {merged.returncode == 0}")
    print(f"  task lifecycle: {task['lifecycle']}, ball: {task.get('ball')}")
    print(f"  ball_prompt: {str(task.get('ball_prompt'))[:160]}")
    return ok


def case_red_gate(box: Sandbox) -> bool:
    """f4: a failing gate blocks the merge."""
    print("\n== case: a branch whose gate is red")
    branch = "sandbox/red-gate"
    worktree = box.branch_worktree(branch, "red-gate")
    # A formatting violation, so the gate's first and cheapest stage fails in a second
    # or two. What is being demonstrated is that a red gate blocks a merge, not which
    # stage went red.
    (worktree / "src" / "agentjobs" / "sandbox_red.py").write_text(
        "def   badly_formatted( ):\n        return    1\n", encoding="utf-8"
    )
    git(worktree, "add", "--", "src/agentjobs/sandbox_red.py")
    git(worktree, "commit", "-qm", "a deliberately unformatted file")

    task_id = box.create_task("Red-gate branch", branch)
    before = head(box.clone, branch)

    result = box.finish(task_id)

    task = box.task(task_id)
    merged = git(box.clone, "merge-base", "--is-ancestor", branch, "main", check=False)
    reason = outcome_of(result)
    ok = (
        result.returncode == 1
        and reason == "gate_failed"
        and merged.returncode != 0
        and task["lifecycle"] == "active"
        and "red gate never merges" in str(task.get("ball_prompt") or "")
    )
    print(f"  stopped at: {reason}")
    print(f"  branch head {before[:8]}; merged into main: {merged.returncode == 0}")
    print(f"  task lifecycle: {task['lifecycle']}, ball: {task.get('ball')}")
    print(f"  ball_prompt: {str(task.get('ball_prompt'))[:200]}")
    return ok


def case_stale_server(box: Sandbox) -> bool:
    """f5: a server that did not come back on the merge is not a finish."""
    print("\n== case: the restart command runs, and the server does not actually restart")
    box.write_dispatch_config(restart=False)
    branch = "sandbox/stale"
    worktree = box.branch_worktree(branch, "stale")
    # Written the way Black would write it. The gate here is the real one, so a sandbox
    # file Black would reformat turns every case downstream of the gate into another
    # red-gate case -- which is what the second run of this script actually did.
    (worktree / "src" / "agentjobs" / "sandbox_served.py").write_text(
        '"""Served code the running process holds in memory, for the sandbox."""\n\n'
        "SANDBOX_SERVED = True\n",
        encoding="utf-8",
    )
    git(worktree, "add", "--", "src/agentjobs/sandbox_served.py")
    git(worktree, "commit", "-qm", "change code the server holds in memory")

    task_id = box.create_task("Served change, no real restart", branch)
    serving_before = box.version().get("source_commit")

    result = box.finish(task_id)

    task = box.task(task_id)
    merged = git(box.clone, "merge-base", "--is-ancestor", branch, "main", check=False)
    serving_after = box.version().get("source_commit")
    prompt = str(task.get("ball_prompt") or "")
    reason = outcome_of(result)
    ok = (
        result.returncode == 1
        and reason == "not_live"
        and merged.returncode == 0
        and serving_after == serving_before
        and task["lifecycle"] == "active"
        and "The merge is done" in prompt
    )
    print(f"  stopped at: {reason}")
    print(f"  merged into main: {merged.returncode == 0} (it should be: the merge is real)")
    print(f"  task still open: {task['lifecycle'] == 'active'}, ball: {task.get('ball')}")
    print(f"  server reported {str(serving_before)[:8]} before and {str(serving_after)[:8]} after")
    print(f"  ball_prompt: {prompt[:240]}")
    box.write_dispatch_config(restart=True)
    return ok


def case_happy(box: Sandbox) -> bool:
    """f1: a clean, green branch merges, closes, restarts and is seen to be live."""
    print("\n== case: a clean branch with a green gate")
    branch = "sandbox/happy"
    worktree = box.branch_worktree(branch, "happy")
    (worktree / "src" / "agentjobs" / "sandbox_feature.py").write_text(
        '"""A sandbox module standing in for served code a merge changes."""\n\n'
        "SANDBOX_FEATURE = True\n",
        encoding="utf-8",
    )
    git(worktree, "add", "--", "src/agentjobs/sandbox_feature.py")
    git(worktree, "commit", "-qm", "feat: a sandbox module the server will hold in memory")

    task_id = box.create_task("Clean green branch", branch)
    serving_before = box.version().get("source_commit")
    print(f"  the server is currently running {str(serving_before)[:8]}")

    started = time.monotonic()
    result = box.finish(task_id)
    elapsed = time.monotonic() - started

    task = box.task(task_id)
    merged = git(box.clone, "merge-base", "--is-ancestor", branch, "main", check=False)
    serving_after = box.version().get("source_commit")
    worktrees_now = git(box.clone, "worktree", "list").stdout
    ok = (
        result.returncode == 0
        and merged.returncode == 0
        and task["lifecycle"] == "closed"
        and task["outcome"] == "completed"
        and str(worktree) not in worktrees_now
        and serving_after != serving_before
    )
    print(f"  merged into main: {merged.returncode == 0}")
    print(f"  task: {task['lifecycle']}/{task.get('outcome')}")
    print(f"  branch entry: {task.get('branches')}")
    print(f"  server was running {str(serving_before)[:8]}, now runs {str(serving_after)[:8]}")
    print(f"  worktree removed: {str(worktree) not in worktrees_now}")
    print(f"  whole finish, gate included: {elapsed:.1f}s")
    return ok


CASE_FUNCTIONS = {
    "conflict": case_conflict,
    "red-gate": case_red_gate,
    "stale-server": case_stale_server,
    "happy": case_happy,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--keep", action="store_true", help="leave the sandbox in place")
    parser.add_argument(
        "--base", type=Path, default=None, help="where to build it (default: a temp dir)"
    )
    args = parser.parse_args(argv)

    branch = git(ROOT, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base = args.base or Path(os.environ.get("TEMP", "/tmp")) / "agentjobs-finish-sandbox"
    remove_tree(base)

    box = Sandbox(base)
    results: Dict[str, bool] = {}
    try:
        box.build(branch)
        box.start_server()
        for name in CASES if args.case == "all" else (args.case,):
            results[name] = CASE_FUNCTIONS[name](box)
    finally:
        box.stop_server()
        if not args.keep:
            remove_tree(base)
        else:
            print(f"\nsandbox kept at {base}")

    print("\n== summary")
    for name, passed in results.items():
        print(f"  {name:<14} {'as specified' if passed else 'NOT AS SPECIFIED'}")
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
