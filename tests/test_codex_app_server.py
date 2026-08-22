"""Tests for the stable Codex App Server session transport."""

from __future__ import annotations

import io
import json
from pathlib import Path

from agentjobs.dispatch.codex_app_server import (
    CodexAppServerProcess,
    CodexSessionSettings,
    parse_session_settings,
)


def test_parse_session_settings_reads_codex_flags_and_posture() -> None:
    settings = parse_session_settings(
        [
            "codex",
            "app-server",
            "--model",
            "gpt-5.6-sol",
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'service_tier="priority"',
        ],
        posture="autonomous",
        project_root=Path("C:/project"),
    )

    assert settings == CodexSessionSettings(
        model="gpt-5.6-sol",
        effort="high",
        approval_policy="never",
        sandbox="danger-full-access",
        service_tier="priority",
    )


def test_app_server_start_and_supervise_use_jsonl_protocol(monkeypatch) -> None:
    output = (
        "\n".join(
            [
                json.dumps({"id": 1, "result": {}}),
                json.dumps({"id": 2, "result": {"thread": {"id": "thr", "sessionId": "sess"}}}),
                json.dumps({"id": 3, "result": {}}),
                json.dumps({"id": 4, "result": {"turn": {"id": "turn"}}}),
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn", "status": "completed"}},
                    }
                ),
            ]
        )
        + "\n"
    )

    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(output)
            self.stderr = io.StringIO()

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout=None) -> None:
            pass

    fake = FakeProcess()
    monkeypatch.setattr(
        "agentjobs.dispatch.codex_app_server.subprocess.Popen", lambda *a, **k: fake
    )
    process = CodexAppServerProcess(
        executable="codex",
        cwd=Path("C:/project"),
        env={},
        settings=CodexSessionSettings(
            "gpt-5.6-luna",
            "high",
            "never",
            "workspace-write",
            ("C:/project/.git", "C:/worktrees"),
            "priority",
        ),
        thread_name="AgentJobs test/task-279",
    )

    started = process.start("do the test")
    completed = process.supervise(turn_id=started.turn_id)
    messages = [json.loads(line) for line in fake.stdin.getvalue().splitlines()]

    assert started.thread_id == "thr"
    assert started.session_id == "sess"
    assert completed["turn"]["status"] == "completed"
    assert [message["method"] for message in messages] == [
        "initialize",
        "initialized",
        "thread/start",
        "thread/name/set",
        "turn/start",
    ]
    assert messages[-1]["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": ["C:/project/.git", "C:/worktrees"],
    }
    assert messages[0]["params"]["capabilities"]["experimentalApi"] is True
    assert messages[2]["params"]["serviceTier"] == "priority"
    assert messages[4]["params"]["serviceTier"] == "priority"


def test_parse_workspace_postures_add_only_git_and_sibling_worktrees() -> None:
    settings = parse_session_settings(
        ["codex", "app-server", "--model", "gpt-5.6-luna"],
        posture="auto",
        project_root=Path("C:/project"),
    )

    assert settings.writable_roots == ("C:\\project\\.git", "C:\\worktrees")


def test_app_server_resumes_a_persisted_thread_before_injecting_follow_up(monkeypatch) -> None:
    output = (
        "\n".join(
            [
                json.dumps({"id": 1, "result": {}}),
                json.dumps(
                    {"id": 2, "result": {"thread": {"id": "thread-existing", "sessionId": "sess"}}}
                ),
                json.dumps({"id": 3, "result": {"turn": {"id": "turn-follow-up"}}}),
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-follow-up", "status": "completed"}},
                    }
                ),
            ]
        )
        + "\n"
    )

    class FakeProcess:
        pid = 1235

        def __init__(self) -> None:
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(output)
            self.stderr = io.StringIO()

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout=None) -> None:
            pass

    fake = FakeProcess()
    monkeypatch.setattr(
        "agentjobs.dispatch.codex_app_server.subprocess.Popen", lambda *a, **k: fake
    )
    process = CodexAppServerProcess(
        executable="codex",
        cwd=Path("C:/project"),
        env={},
        settings=CodexSessionSettings("gpt-5.6-luna", "high", "never", "workspace-write"),
    )

    started = process.start("AgentJobs wake prompt", resume_thread_id="thread-existing")
    process.supervise(turn_id=started.turn_id)
    messages = [json.loads(line) for line in fake.stdin.getvalue().splitlines()]

    assert started.thread_id == "thread-existing"
    assert [message["method"] for message in messages] == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]
    assert messages[2]["params"]["threadId"] == "thread-existing"
    assert messages[3]["params"]["input"][0]["text"] == "AgentJobs wake prompt"
