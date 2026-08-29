"""Stand up the dispatch Output panel in every state it can be in, on its own port.

task-023 changed what that panel reads. It used to tail ``transcript.log`` -- the pty
stream a ``<runner> logs`` subprocess returns -- and a pty stream is a *repaint*: a TUI
draws a space by emitting ``ESC[1C`` rather than a space, so stripping the escape
sequences and keeping what is left deletes every space and every line break. That is how
an approval dialog came to render as ``NewMCPserverfoundinthisproject:agentjobs``.

None of that is visible in a diff, and constructing a dispatched run by hand to look at
it means dispatching one. So three runs are seeded, and **both sides of the change are
here on purpose** -- the same mangled text is in run 2's raw view, so the before and the
after can be compared without reconstructing the before:

    task-201  a real dispatched run's own structured transcript, read end to end
    task-202  a session with no structured transcript: the note, and the raw repaint
    task-203  a run whose commands failed: the failure, in words, on the summary

Every one of them is throwaway. Click anything -- nothing here touches the live corpus,
the 8876 dashboard, or its registry. The data lives under a temporary directory this
process deletes when it stops, and the process redirects its own idea of ``~`` into that
directory so the seeded transcripts cannot land in your real one.

    python scripts/dispatch_output_sandbox.py [port] [--transcript PATH]

``--transcript`` names the JSONL to use for task-201; without it the newest transcript
this machine holds for the AgentJobs checkout is copied in, which is a genuine dispatched
run rather than a fixture.

Stop it with Ctrl-C, or by killing the process. It defaults to a port of its own so it
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * On **task-201**, words and line breaks survive. Read a few narration paragraphs as
    sentences. Then open a summarized run of calls and check the command inside it is
    the command that was actually run.
  * The summary line is the claim: "Edited App.tsx, ran 4 commands" with ``+14 -1``. Open
    it and count -- if the summary and the calls disagree, the summary is wrong.
  * On **task-202**, press *Show raw terminal*. That is the old panel, unchanged, and it
    is deliberately still reachable: when a session dies in a way no renderer models the
    unparsed bytes are the only evidence there is.
  * On **task-203**, a failed command must be findable by *reading*, not by noticing a
    colour: "2 failed" on the summary, "failed" on the call, and the error text under it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import yaml

DEFAULT_PORT = 8903

REAL_HOME = Path.home()
"""Captured before this process redirects its own ``~``, so a real transcript can be found."""


# ----- the seeded transcripts -------------------------------------------------


def _assistant(*blocks: Dict[str, object]) -> Dict[str, object]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _use(call_id: str, name: str, payload: Dict[str, object]) -> Dict[str, object]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": payload}


def _result(
    call_id: str, content: str, *, is_error: bool = False, patch: Optional[List] = None
) -> Dict[str, object]:
    event: Dict[str, object] = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }
    if patch is not None:
        event["toolUseResult"] = {"structuredPatch": patch}
    return event


def failing_run() -> List[Dict[str, object]]:
    """A run that is quietly erroring. It used to look exactly like one that is working."""
    return [
        {"type": "user", "message": {"role": "user", "content": "work task-203"}},
        _assistant({"type": "text", "text": "Wiring the new field through the client."}),
        _assistant(
            _use("c1", "Edit", {"file_path": "C:/repo/frontend/src/api/types.ts"}),
            _use("c2", "Bash", {"command": "npm run build", "description": "Build the bundle"}),
            _use("c3", "Bash", {"command": "npx vitest run", "description": "Run the suite"}),
        ),
        _result("c1", "The file has been updated.", patch=[{"lines": ["+added", "-gone"]}]),
        _result(
            "c2",
            "src/components/DispatchOutput.tsx:214:22 - error TS2339: Property "
            "'entries' does not exist on type 'DispatchRunTailView'.\n\nFound 1 error.",
            is_error=True,
        ),
        _result(
            "c3",
            "FAIL src/components/DispatchOutput.test.tsx\n"
            "  × renders a dialog as sentences\n\nTests  1 failed | 17 passed",
            is_error=True,
        ),
        _assistant({"type": "text", "text": "Both of those failed. Reading the first error."}),
    ]


REPAINT = (
    "\x1b[?25l\x1b[2J\x1b[H\x1b[38;5;245m╭────────────────────────────────╮\x1b[m\n"
    "\x1b[38;5;245m│\x1b[m\x1b[1CNew\x1b[1CMCP\x1b[1Cserver\x1b[1Cfound\x1b[1Cin\x1b[1Cthis"
    "\x1b[1Cproject:\x1b[1Cagentjobs\x1b[m\n"
    "\x1b[3;3H\x1b[38;5;245m│\x1b[m\x1b[1CMCP\x1b[1Cservers\x1b[1Cmay\x1b[1Cexecute\x1b[1Ccode"
    "\x1b[1Cor\x1b[1Caccess\x1b[1Csystem\x1b[1Cresources.\x1b[m\n"
    "\x1b[4;3H\x1b[38;5;245m│\x1b[m\x1b[1C1.\x1b[1CUse\x1b[1Cthis\x1b[1Cserver\x1b[m\n"
    "\x1b[5;3H\x1b[38;5;245m│\x1b[m\x1b[1C2.\x1b[1CDon't\x1b[1Cuse\x1b[1Cthis\x1b[1Cserver\x1b[m\n"
    "\x1b[38;5;245m╰────────────────────────────────╯\x1b[m\n"
    "\x1b[38;5;245m✻\x1b[m\x1b[1CPercolating…\x1b[1C(esc\x1b[1Cto\x1b[1Cinterrupt)\x1b[m\n"
)
"""What the old panel was actually handed, escape sequences and all.

Written as a repaint on purpose: this is the input, not a rendering of it, so the raw
view in the sandbox shows exactly what a reader used to get and no better.
"""


# ----- seeding --------------------------------------------------------------


def seed_tasks(manager) -> None:
    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority

    def make(task_id: str, title: str, prompt: str) -> None:
        manager.create_task(
            id=task_id,
            title=title,
            description=(
                "Seeded so the dispatch Output panel can be looked at against this kind "
                "of run. Nothing here is real work, and every control is safe to press."
            ),
            summary=f"{title}.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="claude",
        )
        manager.claim_task(task_id, agent="claude")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt=prompt,
        )

    make(
        "task-201",
        "A real dispatched run, read through the panel",
        "Open Output. Read the narration as sentences, then open a run of calls and "
        "check the command inside it against the summary line.",
    )
    make(
        "task-202",
        "A session with no structured transcript",
        "Open Output. It should say why there is nothing structured and show the raw "
        "capture underneath. Press 'Show raw terminal' to see the panel this replaced.",
    )
    make(
        "task-203",
        "A run whose commands failed",
        "Open Output. Two commands failed. Find that by reading, not by noticing a colour.",
    )


def seed_run(
    home: Path,
    *,
    run_id: str,
    task_id: str,
    project_id: str,
    session_id: str,
    transcript_log: str = "",
) -> None:
    """One run in the ledger, as the dispatcher would have left it."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": run_id,
                "task_id": task_id,
                "project_id": project_id,
                "mode": "session",
                "driver": "claude",
                "posture": "auto",
                "status": "running",
                "started_at": "2026-08-29T17:26:09.866674+00:00",
                "session_id": session_id,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # What `_start_session` really writes there under `--bg`: the launcher's banner.
    (directory / "stdout.log").write_text(
        f"backgrounded - {session_id} - aj-{task_id}\n", encoding="utf-8"
    )
    if transcript_log:
        (directory / "transcript.log").write_text(transcript_log, encoding="utf-8")


def seed_session_transcript(
    fake_home: Path, project_root: Path, session_id: str, events: List[Dict[str, object]]
) -> None:
    from agentjobs.dispatch.transcript import project_slug

    store = fake_home / ".claude" / "projects" / project_slug(project_root)
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{session_id}-0000-0000-0000-000000000000.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def newest_real_transcript() -> Optional[Path]:
    """The most recent transcript this machine holds for the AgentJobs checkout.

    A genuine dispatched run rather than a fixture, which is the point: a renderer that
    only ever meets transcripts written to suit it has not been tested against the
    format it actually has to read.
    """
    from agentjobs.dispatch.transcript import project_slug

    candidates: List[Path] = []
    projects = REAL_HOME / ".claude" / "projects"
    for name in {project_slug(Path.cwd()), project_slug(Path.cwd().parent)}:
        store = projects / name
        if store.is_dir():
            candidates.extend(store.glob("*.jsonl"))
    if not candidates:
        candidates = list(projects.glob("*agentjobs*/*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_project(root: Path, *, project_id: str, name: str) -> Path:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    seed_tasks(TaskManager(TaskStorage(project_root / "tasks")))
    return project_root


STATES = [
    ("task-201", "a real dispatched run", "narration as sentences; summaries you can open"),
    ("task-202", "no structured transcript", "the note, and 'Show raw terminal' for the old panel"),
    ("task-203", "a run that failed", "'2 failed' on the summary, the error text inside"),
]


def main() -> None:
    argv = sys.argv[1:]
    chosen: Optional[Path] = None
    if "--transcript" in argv:
        index = argv.index("--transcript")
        chosen = Path(argv[index + 1])
        del argv[index : index + 2]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-output-"))
    home = root / "home"
    home.mkdir()
    fake_home = root / "user"
    fake_home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)
    # Redirect this process's idea of ``~`` so the seeded session transcripts land in the
    # sandbox rather than in the real store the server would otherwise read and write.
    os.environ["USERPROFILE"] = str(fake_home)
    os.environ["HOME"] = str(fake_home)

    from agentjobs.projects import ProjectRegistry

    project_id, name = "sandbox-output", "Sandbox: the dispatch Output panel"
    project_root = build_project(root, project_id=project_id, name=name)
    ProjectRegistry(home).add(project_root, project_id=project_id, name=name)

    from agentjobs.dispatch.transcript import project_slug

    real = chosen or newest_real_transcript()
    if real is not None and real.is_file():
        target = fake_home / ".claude" / "projects" / project_slug(project_root)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, target / "real0001-0000-0000-0000-000000000000.jsonl")
        print(f"[output] task-201 is reading a real run: {real}", flush=True)
    else:
        print("[output] no real transcript found; task-201 will show the degraded note", flush=True)

    seed_run(
        home,
        run_id="run_real0001",
        task_id="task-201",
        project_id=project_id,
        session_id="real0001",
    )
    seed_run(
        home,
        run_id="run_norepaint",
        task_id="task-202",
        project_id=project_id,
        session_id="norecord",
        transcript_log=REPAINT,
    )
    seed_run(
        home,
        run_id="run_failing1",
        task_id="task-203",
        project_id=project_id,
        session_id="failing1",
    )
    seed_session_transcript(fake_home, project_root, "failing1", failing_run())

    import uvicorn

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}/tasks"
    print(f"[output] dispatch-output sandbox at http://127.0.0.1:{port}/app/", flush=True)
    for task_id, state, note in STATES:
        print(f"[output]   {state:<26} {note}", flush=True)
        print(f"[output]     {base}/{task_id}", flush=True)
    print(f"[output] throwaway data under {root}", flush=True)
    print("[output] stop with Ctrl-C; the data is deleted with the process.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
