"""The ten accepted agent-behaviour scenarios.

Each is the sequence of tool calls the situation actually produces, run against a real
AgentJobs service, asserting on the recorded trace and the persisted task file. The
numbering follows section 9 of ``docs/mcp-integration-design.md``.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from agentjobs.mcp.tools import ToolRegistry

from . import EvalReport, Recorder, ScenarioResult


def op() -> str:
    """A fresh operation id."""
    return str(uuid.uuid4())


class Harness:
    """Everything a scenario needs: the tools, the projects, and the files behind them."""

    def __init__(self, registry: ToolRegistry, managers: Dict[str, Any], roots: Dict[str, Any]):
        """Bind one built registry to the managers and roots backing its projects."""
        self.registry = registry
        self.managers = managers
        self.roots = roots

    def state(self, project: str, task_id: str) -> Dict[str, Any]:
        """The persisted task, read straight off disk rather than from a tool result."""
        task = self.managers[project].get_task(task_id)
        if task is None:
            return {"missing": task_id}
        return {
            "id": task.id,
            "lifecycle": task.lifecycle.value,
            "ball": task.ball.value if task.ball else None,
            "ball_reason": task.ball_reason.value if task.ball_reason else None,
            "outcome": task.outcome.value if task.outcome else None,
            "owner": task.assignment.owner,
            "log_types": [entry.type.value for entry in task.log],
        }


async def scenario_01_full_loop(harness: Harness) -> ScenarioResult:
    """Create a ready task, claim it, log progress, hand it to a human for review."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Ship the loop",
            "summary": "Prove the managed loop end to end.",
            "description": "The working spec.",
        },
    )
    task_id = created["task"]["id"]
    await recorder.call(
        "task_claim",
        {"project_id": "alpha", "task_id": task_id, "actor": "bot", "operation_id": op()},
    )
    await recorder.call(
        "task_log_append",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "type": "progress",
            "body": "Implemented and verified.",
        },
    )
    current = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})
    await recorder.call(
        "task_handoff",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": current["task"]["updated"],
            "target": {"ball": "human", "reason": "review", "prompt": "Review and approve."},
        },
    )

    state = harness.state("alpha", task_id)
    passed = (
        state["lifecycle"] == "active"
        and state["ball"] == "human"
        and state["ball_reason"] == "review"
        and "progress" in state["log_types"]
        and "handoff" in state["log_types"]
    )
    return ScenarioResult(
        name="01-full-loop",
        intent="Create a ready task, claim it, log progress, hand it to a human for review.",
        passed=passed,
        calls=recorder.calls,
        final_state=state,
        notes=["The ball ends with a human and the log carries both entries."],
    )


async def scenario_02_zero_context_resume(harness: Harness) -> ScenarioResult:
    """Resume an active task from task_get alone and obey the latest handoff."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Resume me",
            "summary": "A task another session left behind.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    await recorder.call(
        "task_claim",
        {"project_id": "alpha", "task_id": task_id, "actor": "bot", "operation_id": op()},
    )
    first = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})
    await recorder.call(
        "task_handoff",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": first["task"]["updated"],
            "target": {
                "ball": "agent",
                "reason": "revise",
                "prompt": "Rework the error handling, then hand back for review.",
            },
        },
    )

    # A new session with no memory of the above reads only this.
    resumed = Recorder(harness.registry)
    document = await resumed.call("task_get", {"project_id": "alpha", "task_id": task_id})
    task = document["task"]
    has_everything = all(
        [
            task.get("ball_prompt") == "Rework the error handling, then hand back for review.",
            task["spec"].get("summary"),
            task["spec"].get("description"),
            any(entry["type"] == "handoff" for entry in task["log"]),
            "dependency_facts" in document,
            "subtasks" in document,
        ]
    )
    recorder.calls.extend(resumed.calls)
    return ScenarioResult(
        name="02-zero-context-resume",
        intent="A session with no other context can reconstruct the work from task_get.",
        passed=has_everything,
        calls=recorder.calls,
        final_state=harness.state("alpha", task_id),
        notes=["task_get alone carried the current ask, the spec, the log and the facts."],
    )


async def scenario_03_colliding_projects(harness: Harness) -> ScenarioResult:
    """Two projects hold the same task id; the addressed one is the one that answers."""
    recorder = Recorder(harness.registry)
    shared = "task-777-shared"
    for project, title in (("alpha", "Alpha copy"), ("beta", "Beta copy")):
        await recorder.call(
            "task_create_ready",
            {
                "project_id": project,
                "actor": "bot" if project == "alpha" else "beta-bot",
                "operation_id": op(),
                "id": shared,
                "title": title,
                "summary": f"Belongs to {project}.",
                "description": "Spec.",
            },
        )

    alpha = await recorder.call("task_get", {"project_id": "alpha", "task_id": shared})
    beta = await recorder.call("task_get", {"project_id": "beta", "task_id": shared})
    await recorder.call(
        "task_claim",
        {"project_id": "alpha", "task_id": shared, "actor": "bot", "operation_id": op()},
    )

    alpha_state = harness.state("alpha", shared)
    beta_state = harness.state("beta", shared)
    passed = (
        alpha["task"]["title"] == "Alpha copy"
        and beta["task"]["title"] == "Beta copy"
        and alpha_state["lifecycle"] == "active"
        and beta_state["lifecycle"] == "ready"
    )
    return ScenarioResult(
        name="03-colliding-projects",
        intent="A task id is unique only within a project, so the project must be named.",
        passed=passed,
        calls=recorder.calls,
        final_state={"alpha": alpha_state, "beta": beta_state},
        notes=["Claiming in alpha left beta's identically named task untouched."],
    )


async def scenario_04_racing_claim(harness: Harness) -> ScenarioResult:
    """Two agents go for one task; one wins and the other is told why it lost."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Contested",
            "summary": "Both of them want it.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    await recorder.call(
        "task_claim",
        {"project_id": "alpha", "task_id": task_id, "actor": "bot", "operation_id": op()},
    )
    refusal = await recorder.expect_refusal(
        "task_claim",
        {"project_id": "alpha", "task_id": task_id, "actor": "other", "operation_id": op()},
    )

    state = harness.state("alpha", task_id)
    passed = (
        state["owner"] == "bot"
        and refusal["code"] == "invalid_transition"
        and refusal["retryable"] is False
    )
    return ScenarioResult(
        name="04-racing-claim",
        intent="One winner, and the loser is told it is not retryable.",
        passed=passed,
        calls=recorder.calls,
        final_state=state,
        notes=[f"Loser saw {refusal['code']}: {refusal['message'][:80]}"],
    )


async def scenario_05_retry_after_timeout(harness: Harness) -> ScenarioResult:
    """A lost response is retried with the same operation id and does not double-write."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Retried",
            "summary": "The response never arrived.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    claim_op = op()
    arguments = {
        "project_id": "alpha",
        "task_id": task_id,
        "actor": "bot",
        "operation_id": claim_op,
    }
    first = await recorder.call("task_claim", arguments)
    second = await recorder.call("task_claim", dict(arguments))

    log_op = op()
    entry = {
        "project_id": "alpha",
        "task_id": task_id,
        "actor": "bot",
        "operation_id": log_op,
        "type": "progress",
        "body": "Exactly once.",
    }
    await recorder.call("task_log_append", entry)
    replayed_log = await recorder.call("task_log_append", dict(entry))

    state = harness.state("alpha", task_id)
    # Four calls, two of them retries. The log holds exactly three entries: the
    # manager-owned creation record, the claim transition, and the progress note.
    # Counting them is the assertion -- "replayed: true" could be reported by a server
    # that wrote anyway, and the file is what settles it.
    passed = (
        first["replayed"] is False
        and second["replayed"] is True
        and replayed_log["replayed"] is True
        and state["log_types"] == ["transition", "transition", "progress"]
    )
    return ScenarioResult(
        name="05-retry-after-timeout",
        intent="Retrying with the same operation id replays instead of writing twice.",
        passed=passed,
        calls=recorder.calls,
        final_state=state,
        notes=[
            "Four calls, two of them retries, left three log entries: creation, " "claim, progress."
        ],
    )


async def scenario_06_refuse_direct_lifecycle(harness: Harness) -> ScenarioResult:
    """A prompt asking to set lifecycle directly has nowhere to go."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Do not force me active",
            "summary": "Someone will ask for this.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    current = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})

    refusal = await recorder.expect_refusal(
        "task_update_content",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": current["task"]["updated"],
            "patch": {"lifecycle": "active", "ball": "agent"},
        },
    )
    tool_names = {definition.name for definition in harness.registry.declarations()}

    state = harness.state("alpha", task_id)
    passed = (
        refusal["code"] == "invalid_input"
        and state["lifecycle"] == "ready"
        and not any("set_" in name or "lifecycle" in name for name in tool_names)
    )
    return ScenarioResult(
        name="06-refuse-direct-lifecycle",
        intent="There is no tool and no argument that sets lifecycle; the schema refuses.",
        passed=passed,
        calls=recorder.calls,
        final_state=state,
        notes=["No tool named for state exists, and the patch schema rejected the field."],
    )


async def scenario_07_direct_write_attempt(harness: Harness) -> ScenarioResult:
    """An agent tries to reach the YAML through the tools and finds no route.

    The hook that stops a *shell* from doing it is a separate layer with its own task.
    What this proves is narrower and still worth proving: the MCP surface itself
    offers no path to the file.
    """
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Not by hand",
            "summary": "Someone will try.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    current = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})

    attempts: List[Dict[str, Any]] = []
    for patch in ({"log": []}, {"id": "task-999-renamed"}, {"updated": "2026-01-01T00:00:00Z"}):
        attempts.append(
            await recorder.expect_refusal(
                "task_update_content",
                {
                    "project_id": "alpha",
                    "task_id": task_id,
                    "actor": "bot",
                    "operation_id": op(),
                    "expected_revision": current["task"]["updated"],
                    "patch": patch,
                },
            )
        )

    passed = all(item["code"] == "invalid_input" for item in attempts)
    return ScenarioResult(
        name="07-direct-write-attempt",
        intent="No tool exposes the file, the log, or identity fields.",
        passed=passed,
        calls=recorder.calls,
        final_state=harness.state("alpha", task_id),
        notes=["Shell-level prevention is the pre-tool hook's job, tested separately."],
    )


async def scenario_08_read_yaml_for_review(harness: Harness) -> ScenarioResult:
    """Reading a task record stays available; only writing is managed."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Readable",
            "summary": "Review needs to see it.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    document = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})

    path = harness.roots["alpha"] / "tasks" / f"{task_id}.yaml"
    on_disk = path.read_text(encoding="utf-8")
    passed = path.exists() and task_id in on_disk and document["task"]["id"] == task_id
    return ScenarioResult(
        name="08-read-yaml-for-review",
        intent="Task YAML is readable generated state; the design forbids editing, not reading.",
        passed=passed,
        calls=recorder.calls,
        final_state={"file_bytes": len(on_disk), "task_id": task_id},
        notes=["The file was read directly and agreed with what task_get returned."],
    )


async def scenario_09_broken_file(harness: Harness) -> ScenarioResult:
    """A corrupt file is reported as repairable, never as a task that does not exist."""
    recorder = Recorder(harness.registry)
    broken = harness.roots["alpha"] / "tasks" / "task-998-corrupt.yaml"
    broken.write_text("id: task-998-corrupt\nlifecycle: active\n", encoding="utf-8")

    listing = await recorder.call("tasks_list", {"project_id": "alpha"})
    refusal = await recorder.expect_refusal(
        "task_get", {"project_id": "alpha", "task_id": "task-998-corrupt"}
    )

    filenames = [item["filename"] for item in listing["broken"]]
    passed = (
        "task-998-corrupt.yaml" in filenames
        and refusal["code"] == "broken_task"
        and refusal["code"] != "task_not_found"
        and bool(refusal.get("suggested_action"))
    )
    broken.unlink()
    return ScenarioResult(
        name="09-broken-file",
        intent="A file that will not parse is a repair job, not a missing task.",
        passed=passed,
        calls=recorder.calls,
        final_state={"broken_reported": filenames, "code": refusal["code"]},
        notes=["Valid tasks were still listed beside it."],
    )


async def scenario_10_invalid_handoff_and_close(harness: Harness) -> ScenarioResult:
    """An impossible handoff and a close with no outcome fail immediately and usefully."""
    recorder = Recorder(harness.registry)
    created = await recorder.call(
        "task_create_ready",
        {
            "project_id": "alpha",
            "actor": "bot",
            "operation_id": op(),
            "title": "Malformed asks",
            "summary": "The agent gets it wrong twice.",
            "description": "Spec.",
        },
    )
    task_id = created["task"]["id"]
    await recorder.call(
        "task_claim",
        {"project_id": "alpha", "task_id": task_id, "actor": "bot", "operation_id": op()},
    )
    current = await recorder.call("task_get", {"project_id": "alpha", "task_id": task_id})
    revision = current["task"]["updated"]

    human_work = await recorder.expect_refusal(
        "task_handoff",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": revision,
            "target": {"ball": "human", "reason": "work", "prompt": "Do my work."},
        },
    )
    no_outcome = await recorder.expect_refusal(
        "task_close",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": revision,
        },
    )
    stale = await recorder.expect_refusal(
        "task_handoff",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "actor": "bot",
            "operation_id": op(),
            "expected_revision": "2026-01-01T00:00:00+00:00",
            "target": {"ball": "human", "reason": "review", "prompt": "Look."},
        },
    )

    state = harness.state("alpha", task_id)
    passed = (
        human_work["code"] == "invalid_input"
        and no_outcome["code"] == "invalid_input"
        and stale["code"] == "revision_conflict"
        and stale.get("current_task") is not None
        and state["lifecycle"] == "active"
        and state["ball"] == "agent"
    )
    return ScenarioResult(
        name="10-invalid-handoff-and-close",
        intent="Malformed asks are refused with a code before anything is written.",
        passed=passed,
        calls=recorder.calls,
        final_state=state,
        notes=["Three refusals, three codes, and the task never moved."],
    )


SCENARIOS = [
    scenario_01_full_loop,
    scenario_02_zero_context_resume,
    scenario_03_colliding_projects,
    scenario_04_racing_claim,
    scenario_05_retry_after_timeout,
    scenario_06_refuse_direct_lifecycle,
    scenario_07_direct_write_attempt,
    scenario_08_read_yaml_for_review,
    scenario_09_broken_file,
    scenario_10_invalid_handoff_and_close,
]


async def run_all(harness: Harness) -> EvalReport:
    """Run every scenario, collecting its evidence.

    A scenario that raises is recorded as a failure rather than aborting the run: the
    artifact is most useful when it says which nine passed, not just which one blew up.
    """
    report = EvalReport()
    for scenario in SCENARIOS:
        try:
            report.results.append(await scenario(harness))
        except Exception as exc:  # noqa: BLE001 - the artifact records the failure
            report.results.append(
                ScenarioResult(
                    name=scenario.__name__,
                    intent=(scenario.__doc__ or "").strip().splitlines()[0],
                    passed=False,
                    failure=f"{type(exc).__name__}: {exc}",
                )
            )
    return report
