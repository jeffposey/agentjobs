"""A dispatched agent identifies as its run, and never as a person (task-331).

Task-329 shipped the resolution order with a verifier that verified nothing, so no
request could resolve ``run`` and loopback was, in practice, one thing: the owner. These
tests are about the half that makes it two.

Three layers, and none of them substitutes for the others:

- **Minting and verification** as pure functions over a temp home, where every refusal
  can be provoked directly -- a forgery, a token for a run that has ended, a run id that
  is really a path.
- **The split, through the real application**, because "with a credential you are that
  run and without one you are the owner" is a property of the request path rather than
  of a function.
- **One end-to-end test that dispatches a real run against a real server on a real
  socket.** It is here because acceptance ac-1 asks for exactly that and a synthesised
  header cannot answer it: the credential the assertion turns on is read by the
  dispatched process out of its own environment, and travels back over HTTP through the
  ordinary client. That test is also where ac-4 is checked, because the artefacts a run
  leaves behind only exist once a run has really been started.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, cast

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.main import app
from agentjobs.dispatch.credentials import (
    CREDENTIAL_ENV,
    CREDENTIAL_FILENAME,
    digest_path,
    mint_run_credential,
    presented_credential,
    revoke_run_credential,
    verify_run_credential,
)
from agentjobs.dispatch.ledger import write_status
from agentjobs.dispatch.runner import RunDirectory, new_run_id, runs_root
from agentjobs.dispatch.session_env import (
    SESSION_SETTINGS_FILENAME,
    SETTINGS_FLAG,
    Delivery,
    deliver_identity,
)
from agentjobs.principals import (
    RUN_CREDENTIAL_HEADER,
    Principal,
    Problem,
    Refusal,
    RunCredential,
    reset_run_credential_verifier,
    resolve_principal,
    set_run_credential_verifier,
)
from agentjobs.projects import ProjectRegistry
from test_dispatch_api_base_end_to_end import build_project, seed_task

LOOPBACK = "127.0.0.1"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ----- a home with runs in it --------------------------------------------------


def make_run(home: Path, *, task_id: str = "task-331", status: str = "running") -> RunDirectory:
    """One run directory, as the dispatcher would have left it."""
    run_id = new_run_id()
    return RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": task_id,
            "project_id": "sandbox",
            "mode": "batch",
            "status": status,
        },
    )


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    return directory


# ----- minting -----------------------------------------------------------------


class TestMinting:
    def test_the_token_names_its_run_and_carries_a_secret(self, home: Path) -> None:
        run = make_run(home)

        token = mint_run_credential(run.path, run.path.name)

        run_id, _, nonce = token.partition(".")
        assert run_id == run.path.name
        assert len(nonce) >= 32, "the secret half must be a real secret, not a marker"

    def test_only_the_digest_is_written_down(self, home: Path) -> None:
        """A ledger that stored tokens would be a directory full of replayable
        credentials for every run this machine has ever had."""
        run = make_run(home)

        token = mint_run_credential(run.path, run.path.name)

        stored = digest_path(run.path).read_text(encoding="utf-8")
        assert token not in stored
        assert token.split(".", 1)[1] not in stored
        assert len(stored.strip()) == 64, "a sha-256 hex digest"

    def test_two_runs_never_share_a_credential(self, home: Path) -> None:
        first, second = make_run(home), make_run(home)

        assert mint_run_credential(first.path, first.path.name) != mint_run_credential(
            second.path, second.path.name
        )

    def test_a_run_id_that_is_really_a_path_mints_nothing(self, home: Path) -> None:
        assert mint_run_credential(home / "x", "../../etc") == ""
        assert mint_run_credential(home / "x", "") == ""

    def test_a_directory_that_cannot_be_written_does_not_raise(self, home: Path) -> None:
        """The same trade `deliver_identity` and `record_phase` make: a run that cannot
        be identified is a gap in an audit trail, and a run that dies because it could
        not be identified is a lost hour of work."""
        blocker = home / "blocked"
        blocker.write_text("I am a file, not a directory", encoding="utf-8")

        assert mint_run_credential(blocker / "run_deadbeef", "run_deadbeef") == ""


# ----- verification ------------------------------------------------------------


class TestVerification:
    def test_a_minted_token_names_the_run_and_its_task(self, home: Path) -> None:
        run = make_run(home, task_id="task-331")
        token = mint_run_credential(run.path, run.path.name)

        verified = verify_run_credential(token, home)

        assert verified == RunCredential(run_id=run.path.name, task_id="task-331")

    def test_a_forged_token_proves_nothing(self, home: Path) -> None:
        run = make_run(home)
        mint_run_credential(run.path, run.path.name)

        assert verify_run_credential(f"{run.path.name}.not-the-secret", home) is None

    def test_a_token_for_a_run_that_does_not_exist_proves_nothing(self, home: Path) -> None:
        assert verify_run_credential("run_deadbeef.anything", home) is None

    @pytest.mark.parametrize(
        "presented",
        ["", "   ", "no-dot-at-all", "run_deadbeef", ".secret", "run_deadbeef."],
    )
    def test_a_malformed_token_proves_nothing(self, presented: str, home: Path) -> None:
        assert verify_run_credential(presented, home) is None

    @pytest.mark.parametrize(
        "run_id",
        ["../../../etc", "..", "run_deadbeef/../..", "RUN_DEADBEEF", "run_zzzzzzzz"],
    )
    def test_a_run_id_that_is_really_a_path_is_refused_before_any_read(
        self, run_id: str, home: Path
    ) -> None:
        """The run id arrives in a request header. Without this it is a path the caller
        chooses, and verification would read a digest from anywhere on the disk."""
        assert verify_run_credential(f"{run_id}.secret", home) is None

    def test_a_digest_that_was_revoked_stops_verifying(self, home: Path) -> None:
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)

        revoke_run_credential(run.path)

        assert not digest_path(run.path).exists()
        assert verify_run_credential(token, home) is None

    @pytest.mark.parametrize("status", ["finished", "cancelled", "failed"])
    def test_a_concluded_runs_credential_is_refused_as_expired(
        self, status: str, home: Path
    ) -> None:
        """ac-3. The refusal is the point: an expired credential must never be *quieter*
        than a forged one, because the fallback for quiet is the owner."""
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)
        # Written straight to the file, so the run ends without the revocation
        # `update_meta` would do. This is the case the status check exists for: a
        # directory whose digest survived its run.
        (run.path / "meta.yaml").write_text(
            yaml.safe_dump(
                {
                    "run_id": run.path.name,
                    "task_id": "task-331",
                    "status": status,
                    "mode": "batch",
                }
            ),
            encoding="utf-8",
        )

        refusal = verify_run_credential(token, home)

        assert isinstance(refusal, Refusal)
        assert refusal.problem is Problem.EXPIRED_RUN_CREDENTIAL
        assert status in refusal.detail

    def test_ending_a_run_through_the_ledger_revokes_its_credential(self, home: Path) -> None:
        """Both writes that can end a run destroy the digest, so the ordinary case never
        leaves a verifiable secret in a directory that will sit there for months."""
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)
        from agentjobs.dispatch.ledger import read_run

        write_status(read_run(run.path), status="finished")

        assert not digest_path(run.path).exists()
        assert verify_run_credential(token, home) is None

    def test_ending_a_run_through_the_directory_revokes_its_credential(self, home: Path) -> None:
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)

        run.update_meta(status="cancelled", outcome="cancelled")

        assert verify_run_credential(token, home) is None


# ----- the split, through the real application ---------------------------------


@pytest.fixture()
def whoami(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A client onto the real app, with the real verifier pointed at a temp home."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    set_run_credential_verifier(verify_run_credential)
    try:
        with TestClient(app, client=(LOOPBACK, 51000)) as client:
            yield client
    finally:
        reset_run_credential_verifier()


def ask(client: Any, token: str = "") -> Dict[str, Any]:
    headers = {RUN_CREDENTIAL_HEADER: token} if token else {}
    response = client.get("/api/whoami", headers=headers)
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


class TestTheSplit:
    """ac-2: the same address, two callers, told apart by one header."""

    def test_loopback_without_a_credential_is_the_owner(self, whoami: Any) -> None:
        answer = ask(whoami)

        assert answer["kind"] == "owner"
        assert answer["source"] == "loopback"
        assert answer["run_id"] is None

    def test_loopback_with_a_credential_is_that_run(self, whoami: Any, home: Path) -> None:
        run = make_run(home, task_id="task-331")
        token = mint_run_credential(run.path, run.path.name)

        answer = ask(whoami, token)

        assert answer["kind"] == "run"
        assert answer["source"] == "run_credential"
        assert answer["run_id"] == run.path.name
        assert answer["task_id"] == "task-331"

    def test_a_run_is_never_attributed_to_a_person(self, whoami: Any, home: Path) -> None:
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)

        answer = ask(whoami, token)

        assert answer["actor_id"] is None
        assert answer["login"] is None

    def test_a_forged_credential_does_not_become_the_owner(self, whoami: Any) -> None:
        answer = ask(whoami, "run_deadbeef.made-this-up")

        assert answer["kind"] is None
        assert answer["problem"] == "unverified_run_credential"

    def test_an_expired_credential_does_not_become_the_owner(self, whoami: Any, home: Path) -> None:
        """ac-3 over the transport. Falling back to a *more* capable principal on expiry
        would invert the whole control, so the expiry is reported as its own problem."""
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)
        run.update_meta(status="finished")

        answer = ask(whoami, token)

        assert answer["kind"] is None
        assert answer["problem"] in {"expired_run_credential", "unverified_run_credential"}

    def test_the_answer_never_echoes_the_credential(self, whoami: Any, home: Path) -> None:
        """A caller learns which run it is, never the token that proved it."""
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)

        answer = ask(whoami, token)

        assert token not in json.dumps(answer)

    def test_a_credential_is_not_believed_from_off_the_machine_either(self, home: Path) -> None:
        """Not a weakening: rule 1 is checked before the front-door rule, so a remote
        caller holding a leaked credential resolves as that run and not as a person.
        Pinned so the precedence cannot be reordered by accident."""
        run = make_run(home)
        token = mint_run_credential(run.path, run.path.name)

        resolution = resolve_principal(
            client_host="100.64.0.7",
            headers={RUN_CREDENTIAL_HEADER: token},
            verifier=lambda presented: verify_run_credential(presented, home),
        )

        assert isinstance(resolution.principal, Principal)
        assert resolution.principal.is_run and not resolution.principal.is_human


# ----- the credential never reaches argv ---------------------------------------


class TestItStaysOutOfArgv:
    """argv is recorded verbatim into `meta.yaml` *and* into the task's dispatch entry,
    which is why a credential moves the settings document out of it and into a file."""

    def test_a_credential_moves_the_document_to_a_file(self, tmp_path: Path) -> None:
        delivered = deliver_identity(
            ["claude.CMD", "--bg", "do the thing"],
            prompt="do the thing",
            directory=tmp_path,
            run_id="run_abc12345",
            credential="run_abc12345.the-secret",
        )

        assert delivered.delivery is Delivery.DELIVERED
        assert (tmp_path / SESSION_SETTINGS_FILENAME).is_file()
        assert "the-secret" not in json.dumps(delivered.argv)
        assert delivered.argv[delivered.argv.index(SETTINGS_FLAG) + 1] == str(
            tmp_path / SESSION_SETTINGS_FILENAME
        )

    def test_the_worker_still_finds_it_in_the_document(self, tmp_path: Path) -> None:
        deliver_identity(
            ["claude.CMD", "--bg", "do the thing"],
            prompt="do the thing",
            directory=tmp_path,
            run_id="run_abc12345",
            credential="run_abc12345.the-secret",
        )

        written = json.loads((tmp_path / SESSION_SETTINGS_FILENAME).read_text(encoding="utf-8"))
        assert written["env"][CREDENTIAL_ENV] == "run_abc12345.the-secret"

    def test_the_record_gets_the_envelope_with_the_value_redacted(self, tmp_path: Path) -> None:
        delivered = deliver_identity(
            ["claude.CMD", "--bg", "do the thing"],
            prompt="do the thing",
            directory=tmp_path,
            run_id="run_abc12345",
            credential="run_abc12345.the-secret",
        )

        assert delivered.document is not None
        env = cast(Dict[str, str], delivered.document["env"])
        assert env[CREDENTIAL_ENV] == "<redacted>"
        assert "the-secret" not in json.dumps(delivered.document)

    def test_a_file_that_cannot_be_written_drops_the_credential_not_the_run_id(
        self, tmp_path: Path
    ) -> None:
        """A run with no credential resolves as the owner, exactly as every run
        dispatched before this existed does -- survivable. A run with no id loses its
        phase records, and a secret in argv is not survivable at all."""
        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a directory", encoding="utf-8")

        delivered = deliver_identity(
            ["claude.CMD", "--bg", "do the thing"],
            prompt="do the thing",
            directory=blocker / "run_abc12345",
            run_id="run_abc12345",
            credential="run_abc12345.the-secret",
        )

        assert delivered.delivery is Delivery.UNCREDENTIALED
        document = json.loads(delivered.argv[delivered.argv.index(SETTINGS_FLAG) + 1])
        assert document["env"]["AGENTJOBS_RUN_ID"] == "run_abc12345"
        assert CREDENTIAL_ENV not in document["env"]
        assert "the-secret" not in json.dumps(delivered.argv)

    def test_without_a_credential_nothing_about_delivery_changes(self, tmp_path: Path) -> None:
        """ac-6: a driver that cannot carry one still dispatches, inline, as before."""
        delivered = deliver_identity(
            ["claude.CMD", "--bg", "do the thing"],
            prompt="do the thing",
            directory=tmp_path,
            run_id="run_abc12345",
        )

        assert delivered.delivery is Delivery.DELIVERED
        assert delivered.document is None
        assert not (tmp_path / SESSION_SETTINGS_FILENAME).exists()


# ----- this process's own credential -------------------------------------------


class TestPresentedCredential:
    def test_a_process_outside_a_run_presents_nothing(self) -> None:
        assert presented_credential({}) == ""
        assert presented_credential({CREDENTIAL_ENV: "   "}) == ""

    def test_a_process_inside_a_run_presents_its_own(self) -> None:
        assert presented_credential({CREDENTIAL_ENV: " run_abc12345.secret "}) == (
            "run_abc12345.secret"
        )

    def test_the_client_attaches_it_when_there_is_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.client import run_credential_headers

        monkeypatch.setenv(CREDENTIAL_ENV, "run_abc12345.secret")
        assert run_credential_headers() == {RUN_CREDENTIAL_HEADER: "run_abc12345.secret"}

    def test_the_client_attaches_nothing_when_there_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which is what keeps the dashboard, the CLI and this suite the owner."""
        from agentjobs.client import run_credential_headers

        monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
        assert run_credential_headers() == {}


# ----- one real run, against a real server on a real socket --------------------


RUNNER = """
import json
import os
import sys

from agentjobs.client import TaskClient

base, out = sys.argv[1], sys.argv[2]
answer = TaskClient(base).service_whoami()
with open(out, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "whoami": answer,
            # Written out so the test can grep the run's artefacts for the exact value.
            # Nothing in AgentJobs does this; a run's credential is its own to keep.
            "credential": os.environ.get("AGENTJOBS_RUN_CREDENTIAL", ""),
        },
        handle,
    )
print("done", flush=True)
"""
"""A runner that asks the service who it is, over the ordinary client."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        port: int = probe.getsockname()[1]
    return port


def write_dispatch_config(home: Path, stub: Path, out: Path, port: int) -> None:
    config: Dict[str, object] = {
        "version": 1,
        "enabled": True,
        "api_base": f"http://{LOOPBACK}:{port}",
        "runners": {
            "asks-who-it-is": {
                "argv": [sys.executable, str(stub), "{api_base}", str(out)],
                "actor": "claude",
                "mode": "batch",
            }
        },
        "projects": {"sandbox": {"enabled": True, "runner": "asks-who-it-is"}},
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


class TestAgainstARealDispatchedRun:
    """ac-1 and ac-4, and the only place either can honestly be claimed.

    A synthesised header proves the resolver; it does not prove that a credential
    reached a dispatched process at all. So this starts a server on a socket, dispatches
    a real run through the API, and has the dispatched process ask the service who it is
    using the credential it found in its own environment. The ~10 seconds is the price of
    the only evidence that answers the acceptance criterion as written.
    """

    def test_a_dispatched_run_is_its_run_and_leaves_no_credential_behind(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        root = tmp_path / "sandbox"
        build_project(root)
        ProjectRegistry(home=home).add(root, project_id="sandbox")
        stub = tmp_path / "asks_who_it_is.py"
        stub.write_text(RUNNER, encoding="utf-8")
        answer_file = tmp_path / "answer.json"
        port = free_port()
        write_dispatch_config(home, stub, answer_file, port)
        task_id = seed_task(root)

        env = dict(os.environ)
        env["AGENTJOBS_HOME"] = str(home)
        for inherited in ("AGENTJOBS_TASKS_DIR", "AGENTJOBS_PROJECT_ROOT", "AGENTJOBS_API_BASE"):
            env.pop(inherited, None)
        # This process may itself be inside a dispatched run. The server must mint the
        # credential the stub presents, not inherit one.
        env.pop(CREDENTIAL_ENV, None)
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agentjobs.api.main:app", "--port", str(port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://{LOOPBACK}:{port}"
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:  # pragma: no cover - only on a very slow machine
                pytest.skip("AgentJobs service did not start in time")

            # The server itself is on loopback with no credential, so it is the owner.
            assert httpx.get(f"{url}/api/whoami", timeout=10).json()["kind"] == "owner"

            started = httpx.post(
                f"{url}/api/projects/sandbox/tasks/{task_id}/dispatch", json={}, timeout=60
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]

            deadline = time.time() + 60
            while time.time() < deadline and not answer_file.exists():
                time.sleep(0.2)
            assert answer_file.exists(), "the dispatched process never answered"
            reported = json.loads(answer_file.read_text(encoding="utf-8"))
        finally:
            server.terminate()
            server.wait(timeout=30)

        # ac-1: the run's own request, resolved by the service, naming this run.
        assert reported["whoami"]["kind"] == "run"
        assert reported["whoami"]["source"] == "run_credential"
        assert reported["whoami"]["run_id"] == run_id
        assert reported["whoami"]["task_id"] == task_id
        assert reported["whoami"]["actor_id"] is None, "a run is never a person"

        # ac-4: the credential the process actually held appears in nothing it left.
        credential = str(reported["credential"])
        assert credential, "the dispatched process found no credential in its environment"
        assert credential.startswith(f"{run_id}.")
        leaked = artefacts_mentioning(credential, root=root, home=home)
        assert leaked == [], f"the credential leaked into {leaked}"
        assert credential not in json.dumps(reported["whoami"])


def artefacts_mentioning(secret: str, *, root: Path, home: Path) -> list[str]:
    """Every file a run leaves behind that contains *secret*.

    The task record, the run's ``meta.yaml``, its captured stdout and stderr, and its
    phase records -- everything under the tasks directory and the run ledger. The
    session-settings document is the one deliberate exception: it *is* the delivery
    channel for a driver whose worker is spawned by a daemon, it is written 0600 beside
    the run, and it is excluded here by name rather than by accident.
    """
    found: list[str] = []
    for directory in (root / "tasks", runs_root(home)):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.name == SESSION_SETTINGS_FILENAME:
                continue
            if path.name == CREDENTIAL_FILENAME:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - a file being written as we read
                continue
            if secret in text:
                found.append(str(path))
    return found
