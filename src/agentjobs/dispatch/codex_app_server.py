"""Small, stable-protocol client for Codex App Server sessions.

AgentJobs' batch Codex runner uses ``codex exec``.  A session runner needs the
bidirectional JSONL App Server protocol instead so the resulting thread is written to
Codex's normal session store and can be opened by Codex Desktop.  This module deliberately
owns only one turn: the caller supervises the process and the persisted run metadata is
the recovery boundary if AgentJobs itself restarts.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence


class CodexAppServerError(Exception):
    """The App Server could not initialize, start a thread, or start its turn."""


@dataclass(frozen=True)
class CodexSessionSettings:
    """Settings that App Server receives for a dispatched thread."""

    model: Optional[str]
    effort: Optional[str]
    approval_policy: str
    sandbox: str
    writable_roots: tuple[str, ...] = ()
    service_tier: Optional[str] = None


@dataclass(frozen=True)
class CodexSessionStarted:
    """Identifiers returned by ``thread/start`` and ``turn/start``."""

    thread_id: str
    session_id: str
    turn_id: str
    pid: int


@dataclass(frozen=True)
class CodexMcpPreflight:
    """Readiness evidence for the required AgentJobs MCP server."""

    server_name: str
    status: str


def _response_error(message: Dict[str, Any], method: str) -> CodexAppServerError:
    error = message.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or repr(error)
    else:
        detail = repr(message)
    return CodexAppServerError(f"Codex App Server {method} failed: {detail}")


class CodexAppServerProcess:
    """One App Server process, initialized and driven over newline-delimited JSON."""

    def __init__(
        self,
        *,
        executable: str,
        cwd: Path,
        env: Dict[str, str],
        settings: CodexSessionSettings,
        service_name: str = "agentjobs",
        thread_name: Optional[str] = None,
        preflight_timeout_seconds: float = 30.0,
    ) -> None:
        self.executable = executable
        self.cwd = cwd
        self.env = env
        self.settings = settings
        self.service_name = service_name
        self.thread_name = thread_name
        self.preflight_timeout_seconds = preflight_timeout_seconds
        self.process: Optional["subprocess.Popen[str]"] = None
        self._next_id = 1
        self._write_lock = threading.Lock()

    @property
    def pid(self) -> int:
        if self.process is None:
            raise CodexAppServerError("Codex App Server has not started.")
        return self.process.pid

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise CodexAppServerError("Codex App Server stdin is unavailable.")
        with self._write_lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _read(self) -> Dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise CodexAppServerError("Codex App Server stdout is unavailable.")
        line = self.process.stdout.readline()
        if not line:
            stderr = ""
            if self.process.stderr is not None:
                stderr = self.process.stderr.read().strip()
            detail = stderr or "the process exited without a response"
            raise CodexAppServerError(f"Codex App Server stopped: {detail}")
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError(
                f"Codex App Server returned non-JSON output: {line.strip()[:300]}"
            ) from exc
        if not isinstance(decoded, dict):
            raise CodexAppServerError("Codex App Server returned a non-object JSON message.")
        return decoded

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        while True:
            message = self._read()
            if message.get("id") != request_id:
                # Notifications, including thread/started, are expected between the
                # request and its response.  They are consumed here; the supervisor
                # receives the turn stream after startup completes.
                continue
            if "error" in message:
                raise _response_error(message, method)
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError(f"Codex App Server {method} returned no result.")
            return result

    def _launch_and_initialize(self) -> None:
        """Start the App Server and complete its required connection handshake."""
        popen_kwargs: dict[str, Any] = {
            "cwd": str(self.cwd),
            "env": self.env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            # Keep console control events sent to AgentJobs' launcher tab from
            # aborting a dispatched Codex turn. Batch dispatch already establishes
            # this boundary; App Server sessions need the same isolation.
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self.process = subprocess.Popen(
            [self.executable, "app-server", "-c", "mcp_servers.agentjobs.required=true"],
            **popen_kwargs,
        )
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.service_name,
                    "title": "AgentJobs Codex session",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized", "params": {}})

    @staticmethod
    def _mcp_status(entry: Dict[str, Any]) -> Optional[str]:
        """Extract the status shape used by supported App Server versions."""
        raw = entry.get("startupStatus", entry.get("status"))
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for key in ("status", "type", "state"):
                value = raw.get(key)
                if isinstance(value, str):
                    return value
        return None

    def preflight_required_mcp(self, *, server_name: str = "agentjobs") -> CodexMcpPreflight:
        """Prove that the required MCP server is ready before starting a task turn.

        This uses App Server's app-scoped MCP readiness surface.  It deliberately
        does not create a task thread or call a task-mutating MCP tool.  The real
        ``thread/start``/``thread/resume`` retains Codex's required-server check as
        the final fail-closed guard.
        """
        result: list[CodexMcpPreflight] = []
        errors: list[BaseException] = []

        def inspect() -> None:
            self._launch_and_initialize()
            response = self._request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})
            entries = response.get("data", response.get("servers", []))
            if not isinstance(entries, list):
                raise CodexAppServerError(
                    "Codex App Server MCP preflight returned no server status list."
                )
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("name") != server_name:
                    continue
                status = self._mcp_status(entry)
                if isinstance(status, str) and status.lower() == "ready":
                    result.append(CodexMcpPreflight(server_name=server_name, status=status))
                    return
                detail = entry.get("failureReason") or entry.get("error") or status or "unknown"
                raise CodexAppServerError(
                    f"Codex App Server required MCP server '{server_name}' is not ready: {detail}"
                )
            raise CodexAppServerError(
                f"Codex App Server required MCP server '{server_name}' was not configured."
            )

        def run_inspection() -> None:
            try:
                inspect()
            except BaseException as exc:  # delivered back to the calling protocol boundary
                errors.append(exc)

        try:
            worker = threading.Thread(target=run_inspection, daemon=True)
            worker.start()
            worker.join(self.preflight_timeout_seconds)
            if worker.is_alive():
                self.terminate()
                raise CodexAppServerError(
                    "Codex App Server MCP preflight timed out after "
                    f"{self.preflight_timeout_seconds:g} seconds."
                )
            if errors:
                raise errors[0]
            if result:
                return result[0]
            raise CodexAppServerError(
                "Codex App Server MCP preflight returned no readiness result."
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexAppServerError(f"Could not start Codex App Server preflight: {exc}") from exc
        finally:
            self.terminate()

    def start(self, prompt: str, *, resume_thread_id: Optional[str] = None) -> CodexSessionStarted:
        """Launch, initialize, and start a turn for a dispatched task.

        ``resume_thread_id`` uses App Server's persisted-thread path.  The process is
        intentionally short-lived, but the conversation is not: ``thread/resume``
        reloads the same Codex Desktop thread before injecting the new AgentJobs wake
        prompt as its next turn.
        """
        try:
            self._launch_and_initialize()
            if resume_thread_id:
                thread_result = self._request(
                    "thread/resume",
                    {
                        "threadId": resume_thread_id,
                        "cwd": str(self.cwd),
                        "approvalPolicy": self.settings.approval_policy,
                        "sandbox": self.settings.sandbox,
                        **(
                            {"serviceTier": self.settings.service_tier}
                            if self.settings.service_tier
                            else {}
                        ),
                    },
                )
            else:
                thread_result = self._request(
                    "thread/start",
                    {
                        **({"model": self.settings.model} if self.settings.model else {}),
                        "cwd": str(self.cwd),
                        "approvalPolicy": self.settings.approval_policy,
                        "sandbox": self.settings.sandbox,
                        "serviceName": self.service_name,
                        **(
                            {"serviceTier": self.settings.service_tier}
                            if self.settings.service_tier
                            else {}
                        ),
                    },
                )
            thread = thread_result.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise CodexAppServerError("Codex App Server thread/start returned no thread id.")
            thread_id = thread["id"]
            session_id = thread.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                session_id = thread_id
            if self.thread_name and not resume_thread_id:
                self._request("thread/name/set", {"threadId": thread_id, "name": self.thread_name})
            turn_result = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    **({"model": self.settings.model} if self.settings.model else {}),
                    **({"effort": self.settings.effort} if self.settings.effort else {}),
                    **(
                        {"serviceTier": self.settings.service_tier}
                        if self.settings.service_tier
                        else {}
                    ),
                    "cwd": str(self.cwd),
                    "approvalPolicy": self.settings.approval_policy,
                    "sandboxPolicy": {
                        "type": {
                            "read-only": "readOnly",
                            "workspace-write": "workspaceWrite",
                            "danger-full-access": "dangerFullAccess",
                        }[self.settings.sandbox]
                    }
                    | (
                        {"writableRoots": list(self.settings.writable_roots)}
                        if self.settings.sandbox == "workspace-write"
                        and self.settings.writable_roots
                        else {}
                    ),
                },
            )
            turn = turn_result.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise CodexAppServerError("Codex App Server turn/start returned no turn id.")
            return CodexSessionStarted(thread_id, session_id, turn["id"], self.pid)
        except (OSError, subprocess.SubprocessError) as exc:
            self.terminate()
            raise CodexAppServerError(f"Could not start Codex App Server: {exc}") from exc
        except BaseException:
            self.terminate()
            raise

    def supervise(
        self,
        *,
        turn_id: str,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Read notifications until the started turn completes."""
        if self.process is None:
            raise CodexAppServerError("Codex App Server has not started.")
        if timeout is not None:
            # ``communicate`` cannot be used because we must consume JSONL incrementally;
            # the caller's supervisor supplies its own wall-clock timeout.
            _ = timeout
        while True:
            message = self._read()
            if on_message is not None:
                on_message(message)
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                continue
            return params

    def terminate(self) -> None:
        """Stop the child process, if it still exists."""
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass


def parse_session_settings(
    argv: Sequence[str], *, posture: str, project_root: Path
) -> CodexSessionSettings:
    """Extract model/effort from a configured Codex session recipe.

    ``argv`` remains an ordinary dispatch recipe so config validation, selection and
    audit records keep their existing shape. App Server receives the equivalent typed
    values rather than an unsupported CLI prompt argument.
    """
    model: Optional[str] = None
    effort: Optional[str] = None
    service_tier: Optional[str] = None
    for index, element in enumerate(argv):
        if element in {"--model", "-m"} and index + 1 < len(argv):
            model = argv[index + 1]
        if element in {"--reasoning-effort", "--effort"} and index + 1 < len(argv):
            effort = argv[index + 1]
        if element == "model_reasoning_effort" and index + 1 < len(argv):
            effort = argv[index + 1].strip("\"'")
        if element.startswith("model_reasoning_effort="):
            effort = element.split("=", 1)[1].strip("\"'")
        if element.startswith("service_tier="):
            service_tier = element.split("=", 1)[1].strip("\"'")
    sandbox = {
        "read_only": "read-only",
        "auto": "workspace-write",
        "autonomous": "danger-full-access",
        "supervised": "workspace-write",
    }.get(posture)
    if sandbox is None:
        raise CodexAppServerError(f"Unsupported AgentJobs posture for Codex session: {posture}")
    approval = "on-request" if posture == "supervised" else "never"
    writable_roots: tuple[str, ...] = ()
    if sandbox == "workspace-write":
        # Codex's managed workspace profile intentionally makes .git read-only.
        # AgentJobs requires the dispatched agent to create its own sibling
        # worktree, so grant only the metadata directory and the prescribed
        # worktree container.  The project root is already writable under the
        # workspace-write sandbox and is not repeated here.
        writable_roots = (
            str((project_root / ".git").resolve()),
            str((project_root.parent / "worktrees").resolve()),
        )
    return CodexSessionSettings(
        model=model,
        effort=effort,
        approval_policy=approval,
        sandbox=sandbox,
        writable_roots=writable_roots,
        service_tier=service_tier,
    )
