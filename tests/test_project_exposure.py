"""A local-only project stays local, over HTTP, including through its transcripts.

Two halves, and both are load-bearing.

``TestTheRule`` exercises :mod:`agentjobs.exposure` directly, because the rule has to be
arguable without a transport -- the same split :mod:`agentjobs.capabilities` makes.

Everything after it goes in **over HTTP**, from two sockets that differ only in whether
they carry the header the front door sets. That is how the audit established the finding
it is answering, and it is the only way to establish this one: the question is not
whether a predicate returns False, it is whether a phone can still reach the bytes. Every
refusal is asserted as **absent** rather than forbidden, and the sentence is asserted to
be the one an unregistered id gets -- a 404 whose body differs from the unknown-id 404 by
one word would hand back exactly the disclosure it exists to prevent.

``TestAnOwnerLosesNothing`` runs the same requests from the same socket without the
header, so "an owner loses nothing" is asserted rather than assumed, and a mistake that
hides Jeff's own projects from his own machine fails here rather than on a phone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.transcript import (
    CLAUDE_HOME_DIRNAME,
    PROJECTS_DIRNAME,
    SOURCE_JSONL,
    project_slug,
)
from agentjobs.front_door import SECRET_ENV, reset_cache as reset_front_door_cache
from agentjobs.exposure import (
    DEFAULT_VISIBILITY,
    VISIBILITY_KEY,
    Visibility,
    readable_by,
    visibility_of,
)
from agentjobs.manager import TaskManager
from agentjobs.principals import (
    FRONT_DOOR_HEADER,
    IDENTITY_HEADER,
    Principal,
    PrincipalKind,
    PrincipalSource,
    resolve_principal,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

LOOPBACK = "127.0.0.1"

FRONT_DOOR_SECRET = "a-front-door-secret-for-this-test"
"""What the proxy proves itself with. Since task-244 loopback alone is not the front
door, so a test that only sets the identity header resolves to nobody rather than to a
``tailnet`` principal -- and would then assert the right refusals for the wrong reason."""

REMOTE_HEADERS = {
    FRONT_DOOR_HEADER: FRONT_DOOR_SECRET,
    IDENTITY_HEADER: "jeff@example.com",
}

OPEN_PROJECT = "alpha"
HIDDEN_PROJECT = "ledger"
OPEN_TASK = "task-001"
HIDDEN_TASK = "task-900"
HIDDEN_RUN = "run_hidden"
HIDDEN_SESSION = "553f321b"
OPEN_RUN = "run_open"
OPEN_SESSION = "77c1a204"
OPEN_NARRATION = "Rebased onto main and ran the gate."

SECRET = "the rent ledger for 14 Elm Row"
"""Written into the hidden project's task and into its run transcript, so every test
below can assert on the actual bytes rather than on a status code alone."""


# ----- the rule, without a transport ------------------------------------------


class TestTheRule:
    """`agentjobs.exposure`, exercised the way `capabilities` is: as a pure function."""

    def test_a_project_that_says_nothing_is_shared(self) -> None:
        assert visibility_of({"project_name": "Anything"}) is Visibility.SHARED
        assert visibility_of({}) is DEFAULT_VISIBILITY
        assert visibility_of(None) is DEFAULT_VISIBILITY

    def test_the_configured_values_are_read(self) -> None:
        assert visibility_of({VISIBILITY_KEY: "local"}) is Visibility.LOCAL
        assert visibility_of({VISIBILITY_KEY: "  SHARED "}) is Visibility.SHARED

    @pytest.mark.parametrize("written", ["locl", "private", "no", True, 0, ["local"]])
    def test_a_value_nobody_recognises_is_hidden_not_shared(self, written: Any) -> None:
        """The only way to get one is to have tried to say something. Fail closed."""
        assert visibility_of({VISIBILITY_KEY: written}) is Visibility.LOCAL

    def test_local_means_on_this_machine_which_a_run_is(self) -> None:
        """A dispatched agent has the files. Refusing it over HTTP protects nothing."""
        run = Principal(
            kind=PrincipalKind.RUN,
            source=PrincipalSource.RUN_CREDENTIAL,
            run_id="run_x",
            task_id="task-1",
        )
        owner = Principal(kind=PrincipalKind.OWNER, source=PrincipalSource.LOOPBACK)
        remote = Principal(
            kind=PrincipalKind.TAILNET,
            source=PrincipalSource.PROVEN_HEADER,
            login="jeff@example.com",
        )

        assert readable_by(Visibility.LOCAL, owner) is True
        assert readable_by(Visibility.LOCAL, run) is True
        assert readable_by(Visibility.LOCAL, remote) is False
        assert all(readable_by(Visibility.SHARED, who) for who in (owner, run, remote))

    def test_a_caller_who_resolved_to_nobody_is_not_on_this_machine(self) -> None:
        assert readable_by(Visibility.LOCAL, None) is False
        assert readable_by(Visibility.SHARED, None) is True


# ----- a machine serving one open project and one hidden one -------------------


def _make_project(tmp_path: Path, home: Path, project_id: str, **extra: Any) -> Path:
    root = tmp_path / project_id
    (root / ".agentjobs").mkdir(parents=True)
    config: Dict[str, Any] = {
        "project_name": f"{project_id.title()} Project",
        "tasks_directory": "tasks",
        "actors": [{"name": "Jeff Posey", "kind": "human"}, {"name": "claude", "kind": "agent"}],
        "default_user": "Jeff Posey",
    }
    config.update(extra)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "tasks").mkdir()
    ProjectRegistry(home=home).add(root, project_id=project_id, name=config["project_name"])
    return root


def _make_task(root: Path, task_id: str, title: str) -> None:
    TaskManager(TaskStorage(root / "tasks")).create_task(
        id=task_id,
        title=title,
        summary=f"{title}.",
        description=f"{title}, at length.",
        actor="claude",
    )


def _write_run(home: Path, run_id: str, **meta: Any) -> Path:
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    started = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    body: Dict[str, Any] = {"run_id": run_id, "started_at": started, **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return directory


def _write_session_transcript(session_home: Path, cwd: Path, session_id: str, text: str) -> Path:
    """A runner's own JSONL for one session, in the store the reader searches.

    This is the file AgentJobs does not own, and the reason exposure cannot be a property
    of the caller: its contents are whatever that session happened to look at.
    """
    store = session_home / CLAUDE_HOME_DIRNAME / PROJECTS_DIRNAME / project_slug(cwd)
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{session_id}-aefe-4f21-9c02-0d1f2b3c4d5e.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tuple[Path, Path]]:
    """Two registered projects -- one shared, one ``visibility: local`` -- and a run.

    The hidden project gets the whole apparatus a real one has: a task, a live session
    run in the ledger, and a transcript in the runner's store under its own root. Half
    the point of this task is that those are three different disclosure paths.
    """
    home = tmp_path / "home"
    home.mkdir()
    session_home = tmp_path / "sessionhome"
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.setenv(SECRET_ENV, FRONT_DOOR_SECRET)
    reset_front_door_cache()
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentjobs.api.routes.dispatch._session_home", lambda: session_home)
    reset_dependency_cache()

    _make_project(tmp_path, home, OPEN_PROJECT)
    hidden = _make_project(tmp_path, home, HIDDEN_PROJECT, **{VISIBILITY_KEY: "local"})
    _make_task(tmp_path / OPEN_PROJECT, OPEN_TASK, "Teach the queue to count")
    _make_task(hidden, HIDDEN_TASK, SECRET)
    _write_run(
        home,
        HIDDEN_RUN,
        task_id=HIDDEN_TASK,
        project_id=HIDDEN_PROJECT,
        mode="session",
        status="running",
        session_id=HIDDEN_SESSION,
        posture="auto",
    )
    _write_session_transcript(session_home, hidden, HIDDEN_SESSION, SECRET)
    _write_run(
        home,
        OPEN_RUN,
        task_id=OPEN_TASK,
        project_id=OPEN_PROJECT,
        mode="session",
        status="running",
        session_id=OPEN_SESSION,
        posture="auto",
    )
    _write_session_transcript(session_home, tmp_path / OPEN_PROJECT, OPEN_SESSION, OPEN_NARRATION)

    yield tmp_path, home

    reset_dependency_cache()
    reset_front_door_cache()


def remote() -> TestClient:
    """A tailnet peer: through the proven front door, carrying a proven identity."""
    return TestClient(app, client=(LOOPBACK, 51001), headers=REMOTE_HEADERS)


def owner() -> TestClient:
    """The person at this machine: the same socket, neither header."""
    return TestClient(app, client=(LOOPBACK, 51002))


class TestTheTwoClientsAreWhoTheyClaimToBe:
    """The fixture's own premise, asserted rather than assumed.

    Every refusal below would also be produced by a request that resolved to *nobody*,
    and since task-244 that is exactly what loopback plus an identity header gets you
    without the front-door secret. So this suite would pass for the wrong reason and
    prove nothing about a tailnet caller. One test stops that.
    """

    def test_the_remote_client_resolves_tailnet(self, machine) -> None:
        resolution = resolve_principal(
            client_host=LOOPBACK,
            headers=REMOTE_HEADERS,
            front_door_secret=FRONT_DOOR_SECRET,
        )

        assert resolution.principal is not None, resolution.detail
        assert resolution.principal.kind is PrincipalKind.TAILNET

    def test_the_owner_client_resolves_owner(self, machine) -> None:
        resolution = resolve_principal(client_host=LOOPBACK, headers={})

        assert resolution.principal is not None, resolution.detail
        assert resolution.principal.kind is PrincipalKind.OWNER


def _unknown_project_404(client: TestClient) -> Dict[str, Any]:
    """What an id that was never registered answers, for comparison."""
    response = client.get("/api/projects/nosuchproject/tasks")
    assert response.status_code == 404, response.text
    body: Dict[str, Any] = response.json()
    return body


# ----- ac-1, ac-4: the tasks ---------------------------------------------------


class TestAHiddenProjectIsAbsent:
    def test_it_is_not_in_the_project_listing(self, machine) -> None:
        payload = remote().get("/api/projects").json()

        assert [project["id"] for project in payload] == [OPEN_PROJECT]

    def test_its_tasks_are_not_served(self, machine) -> None:
        response = remote().get(f"/api/projects/{HIDDEN_PROJECT}/tasks")

        assert response.status_code == 404
        assert SECRET not in response.text

    def test_one_of_its_tasks_by_id_is_not_served(self, machine) -> None:
        response = remote().get(f"/api/projects/{HIDDEN_PROJECT}/tasks/{HIDDEN_TASK}")

        assert response.status_code == 404
        assert SECRET not in response.text

    def test_the_refusal_is_indistinguishable_from_never_having_existed(self, machine) -> None:
        """ac-4. A 403 here, or a different sentence, tells the caller it is there."""
        client = remote()

        hidden = client.get(f"/api/projects/{HIDDEN_PROJECT}/tasks")
        unknown = _unknown_project_404(client)

        assert hidden.status_code == 404
        assert hidden.json()["detail"] == unknown["detail"].replace("nosuchproject", HIDDEN_PROJECT)

    def test_the_unknown_project_sentence_does_not_name_the_hidden_one(self, machine) -> None:
        """The registry's own sentence lists every registered id. That one leaks."""
        detail = _unknown_project_404(remote())["detail"]

        assert OPEN_PROJECT in detail
        assert HIDDEN_PROJECT not in detail

    def test_writes_are_refused_too_and_change_nothing(self, machine) -> None:
        response = remote().post(
            f"/api/projects/{HIDDEN_PROJECT}/tasks/{HIDDEN_TASK}/log",
            json={"actor": "claude", "type": "progress", "body": "reached in"},
        )

        assert response.status_code == 404


# ----- ac-1, ac-2: the runs and their transcripts ------------------------------


class TestAHiddenProjectsRunsAreAbsent:
    def test_its_run_output_is_not_served(self, machine) -> None:
        response = remote().get(f"/api/projects/{HIDDEN_PROJECT}/dispatch/runs/{HIDDEN_RUN}/output")

        assert response.status_code == 404

    def test_its_run_transcript_is_not_served(self, machine) -> None:
        """ac-2, asked of the transcript route itself rather than inferred."""
        response = remote().get(
            f"/api/projects/{HIDDEN_PROJECT}/dispatch/runs/{HIDDEN_RUN}/transcript"
        )

        assert response.status_code == 404
        assert SECRET not in response.text

    def test_its_run_tail_is_not_served(self, machine) -> None:
        response = remote().get(f"/api/projects/{HIDDEN_PROJECT}/dispatch/runs/{HIDDEN_RUN}/tail")

        assert response.status_code == 404

    def test_its_run_is_not_listed_by_the_project_that_can_be_seen(self, machine) -> None:
        """The open project must not become a side door onto the hidden one's ledger."""
        response = remote().get(f"/api/projects/{OPEN_PROJECT}/dispatch/runs")

        assert response.status_code == 200
        assert [row["run_id"] for row in response.json()] == [OPEN_RUN]

    def test_the_transcript_is_not_reachable_through_the_open_project(self, machine) -> None:
        """The run belongs to the hidden project; the URL naming another does not help."""
        response = remote().get(
            f"/api/projects/{OPEN_PROJECT}/dispatch/runs/{HIDDEN_RUN}/transcript"
        )

        assert response.status_code == 404
        assert SECRET not in response.text

    def test_it_is_absent_from_the_machine_wide_live_runs(self, machine) -> None:
        """The open project's run is there; the hidden one's is not, nor is its task."""
        body = remote().get("/api/runs/live").json()

        assert [row["run_id"] for row in body["runs"]] == [OPEN_RUN]
        assert SECRET not in json.dumps(body)

    def test_the_capacity_still_counts_it(self, machine) -> None:
        """The rows are 'what may I read'; the count is 'why can I not dispatch'.

        Two runs are live and a remote caller may see one of them. It is still told the
        machine has two slots in use, because that is why its next dispatch may be
        refused -- and because a surface that subtracted them would be the one place in
        the system able to disagree with `dispatch/guards.py` about capacity.
        """
        body = remote().get("/api/runs/live").json()

        assert len(body["runs"]) == 1
        assert body["occupied"] == 2


# ----- ac-3: the cross-project view is filtered, not denied --------------------


class TestAllTasksIsFilteredRatherThanDenied:
    def test_a_remote_caller_still_gets_the_route_and_the_open_project(self, machine) -> None:
        response = remote().get("/api/all/tasks")

        assert response.status_code == 200
        rows = response.json()
        assert [row["project_id"] for row in rows] == [OPEN_PROJECT]

    def test_the_hidden_project_is_not_in_it(self, machine) -> None:
        body = remote().get("/api/all/tasks").text

        assert SECRET not in body
        assert HIDDEN_PROJECT not in body

    def test_asking_for_the_hidden_project_by_name_returns_nothing(self, machine) -> None:
        response = remote().get("/api/all/tasks", params={"project": HIDDEN_PROJECT})

        assert response.status_code == 200
        assert response.json() == []


# ----- the constraint that matters most ----------------------------------------


class TestAnOwnerLosesNothing:
    """The same requests from the same socket, without the front door's header."""

    def test_every_project_is_listed(self, machine) -> None:
        payload = owner().get("/api/projects").json()

        assert sorted(project["id"] for project in payload) == [OPEN_PROJECT, HIDDEN_PROJECT]

    def test_the_hidden_projects_tasks_are_served(self, machine) -> None:
        response = owner().get(f"/api/projects/{HIDDEN_PROJECT}/tasks/{HIDDEN_TASK}")

        assert response.status_code == 200, response.text
        assert response.json()["title"] == SECRET

    def test_the_hidden_projects_run_transcript_is_served(self, machine) -> None:
        response = owner().get(
            f"/api/projects/{HIDDEN_PROJECT}/dispatch/runs/{HIDDEN_RUN}/transcript"
        )

        assert response.status_code == 200, response.text
        assert SECRET in response.text

    def test_all_tasks_still_spans_every_project(self, machine) -> None:
        rows = owner().get("/api/all/tasks").json()

        assert sorted({row["project_id"] for row in rows}) == [OPEN_PROJECT, HIDDEN_PROJECT]

    def test_the_hidden_projects_run_is_listed_machine_wide(self, machine) -> None:
        body = owner().get("/api/runs/live").json()

        assert sorted(row["run_id"] for row in body["runs"]) == sorted([HIDDEN_RUN, OPEN_RUN])
        assert body["occupied"] == 2


# ----- an exposed project keeps working, which is the line this task holds ------


class TestAnExposedProjectIsUntouched:
    def test_its_tasks_are_served_to_a_remote_caller(self, machine) -> None:
        response = remote().get(f"/api/projects/{OPEN_PROJECT}/tasks/{OPEN_TASK}")

        assert response.status_code == 200, response.text
        assert response.json()["id"] == OPEN_TASK

    def test_its_dispatch_state_is_served_to_a_remote_caller(self, machine) -> None:
        response = remote().get(f"/api/projects/{OPEN_PROJECT}/dispatch")

        assert response.status_code == 200, response.text
        assert response.json()["project_id"] == OPEN_PROJECT

    def test_its_structured_transcript_still_renders_for_a_remote_caller(self, machine) -> None:
        """ac-5, in the suite: the panel's own data, over the tailnet, for a real run.

        Asserted on the rendered entry rather than on a 200, because a transcript route
        that answers ``source: none`` with an empty list is also a 200 and is exactly
        what a broken lookup looks like.
        """
        response = remote().get(f"/api/projects/{OPEN_PROJECT}/dispatch/runs/{OPEN_RUN}/transcript")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source"] == SOURCE_JSONL
        assert OPEN_NARRATION in " ".join(entry["text"] for entry in body["entries"])

    def test_its_run_tail_and_output_still_answer_a_remote_caller(self, machine) -> None:
        client = remote()

        for suffix in ("tail", "output"):
            response = client.get(f"/api/projects/{OPEN_PROJECT}/dispatch/runs/{OPEN_RUN}/{suffix}")
            assert response.status_code == 200, f"{suffix}: {response.text}"

    def test_a_remote_caller_may_still_write_to_it(self, machine) -> None:
        """Exposure narrows *which projects*, never what a human may do in one."""
        response = remote().post(
            f"/api/projects/{OPEN_PROJECT}/tasks/{OPEN_TASK}/log",
            json={"actor": "claude", "type": "progress", "body": "still reachable"},
        )

        assert response.status_code in (200, 201), response.text
