"""
Minimal webhook listener that auto-launches VS Code when AgentJobs
tasks change to READY for Codex.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import subprocess
from pathlib import Path

from flask import Flask, Request, abort, request

SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
TASKS_DIR = Path(os.environ.get("TASKS_DIR", "tasks/agentjobs"))

app = Flask(__name__)


def verify_signature(req: Request) -> None:
    header = req.headers.get("X-Hub-Signature-256")
    if not header:
        abort(401, "Missing signature header.")
    payload = req.get_data()
    expected = "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        abort(401, "Invalid signature.")


def open_task_in_code(task_id: str) -> None:
    task_path = TASKS_DIR / f"{task_id}.yaml"
    if not task_path.exists():
        print(f"[WARN] Task file {task_path} not found.")
        return
    subprocess.Popen(["code", str(task_path)])  # noqa: PLW1510


@app.post("/webhook")
def webhook():
    verify_signature(request)
    data = request.json or {}
    event = data.get("event")
    task = data.get("task", {})
    print(f"[INFO] Received {event}: {json.dumps(data, indent=2)}")

    if event == "task.status_changed":
        if task.get("status") == "ready" and task.get("assigned_to") == "Codex":
            open_task_in_code(task["id"])

    return {"status": "ok"}


if __name__ == "__main__":
    app.run(port=5000)
