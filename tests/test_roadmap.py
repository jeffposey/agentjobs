"""What the published roadmap projects, and what it must never carry.

The interesting assertions here are all about the *boundary*. The rendering is a
handful of f-strings and would be pointless to pin line by line; what is worth a test is
the set of decisions a later change could quietly reverse -- that a draft is counted
rather than listed, that a closed task is gone, that the file has no clock in it, and
above all that nothing a record carries outside the published fields can reach a public
page.

The leak tests are the ones to keep honest. They assert on a *refusal*: the render
raises rather than writing a file, because a published artefact with a home directory in
it cannot be recalled by fixing it afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Branch,
    Dependency,
    DependencyType,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.roadmap import (
    PUBLISHED_LIFECYCLES,
    Leak,
    RoadmapLeakError,
    draft_count,
    leaks,
    published,
    published_fields,
    render,
)

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def make_task(
    task_id: str,
    *,
    lifecycle: Lifecycle = Lifecycle.READY,
    priority: Priority = Priority.MEDIUM,
    position: int | None = None,
    title: str | None = None,
    summary: str | None = None,
    dependencies: list[Dependency] | None = None,
    parent: str | None = None,
    archived: bool = False,
) -> Task:
    """A minimally-valid task, varying only what a roadmap reads."""
    closed = lifecycle is Lifecycle.CLOSED
    return Task(
        id=task_id,
        title=title or f"Title of {task_id}",
        created=NOW,
        updated=NOW,
        lifecycle=lifecycle,
        ball=None if closed else Ball.AGENT,
        ball_reason=None if closed else BallReason.AVAILABLE,
        outcome=Outcome.COMPLETED if closed else None,
        archived=archived,
        priority=priority,
        queue_position=None if closed else (position or int(task_id.split("-")[1]) * 100),
        category="general",
        assignment=Assignment(owner="claude" if lifecycle is Lifecycle.ACTIVE else None),
        parent=parent,
        dependencies=dependencies or [],
        spec=Spec(summary=summary or f"Summary of {task_id}.", description="Body."),
    )


class TestWhatIsListed:
    def test_a_draft_is_counted_rather_than_listed(self) -> None:
        """An unspecified idea is not a plan, and publishing it as one misleads.

        Counting it is the other half: a roadmap that simply dropped 29 records would
        imply the backlog is smaller than it is.
        """
        tasks = [make_task("task-001"), make_task("task-002", lifecycle=Lifecycle.DRAFT)]
        text = render(tasks)

        assert "task-001" in text
        assert "task-002" not in text
        assert "1 further open task is a draft" in text
        assert draft_count(tasks) == 1

    def test_a_closed_task_is_gone_entirely(self) -> None:
        tasks = [make_task("task-001"), make_task("task-002", lifecycle=Lifecycle.CLOSED)]
        text = render(tasks)

        assert "task-002" not in text
        assert "further open" not in text

    def test_an_archived_task_is_neither_listed_nor_counted(self) -> None:
        tasks = [
            make_task("task-001"),
            make_task("task-002", lifecycle=Lifecycle.DRAFT, archived=True),
        ]
        text = render(tasks)

        assert "task-002" not in text
        assert "further open" not in text

    def test_an_active_task_is_listed_and_marked(self) -> None:
        """What is being worked right now is the one bit of workflow state published."""
        text = render([make_task("task-001", lifecycle=Lifecycle.ACTIVE)])

        assert "task-001" in text
        assert "in progress" in text

    def test_the_published_lifecycles_are_the_claimable_ones(self) -> None:
        """Pinned because widening this is how workflow state creeps into a public page."""
        assert PUBLISHED_LIFECYCLES == (Lifecycle.READY, Lifecycle.ACTIVE)


class TestOrder:
    def test_bands_run_critical_to_low(self) -> None:
        text = render(
            [
                make_task("task-001", priority=Priority.LOW),
                make_task("task-002", priority=Priority.CRITICAL),
                make_task("task-003", priority=Priority.MEDIUM),
                make_task("task-004", priority=Priority.HIGH),
            ]
        )
        headings = [line for line in text.splitlines() if line.startswith("## ")]

        assert headings == ["## Critical (1)", "## High (1)", "## Medium (1)", "## Low (1)"]

    def test_within_a_band_the_order_is_the_queues(self) -> None:
        """Not the id order, and not the order the store happened to return."""
        tasks = [
            make_task("task-001", position=300),
            make_task("task-002", position=100),
            make_task("task-003", position=200),
        ]
        listed = [task.id for task in published(tasks)]

        assert listed == ["task-002", "task-003", "task-001"]


class TestRelations:
    def test_an_unmet_need_is_published(self) -> None:
        text = render(
            [
                make_task(
                    "task-002",
                    dependencies=[Dependency(task="task-001", type=DependencyType.NEEDS)],
                ),
                make_task("task-001"),
            ]
        )

        assert "needs task-001" in text

    def test_a_satisfied_need_is_not(self) -> None:
        """A closed prerequisite shaped the order once and tells a reader nothing now."""
        text = render(
            [
                make_task(
                    "task-002",
                    dependencies=[Dependency(task="task-001", type=DependencyType.NEEDS)],
                ),
                make_task("task-001", lifecycle=Lifecycle.CLOSED),
            ]
        )

        assert "needs task-001" not in text

    def test_only_needs_is_published_not_blocks_or_related(self) -> None:
        text = render(
            [
                make_task(
                    "task-002",
                    dependencies=[
                        Dependency(task="task-001", type=DependencyType.BLOCKS),
                        Dependency(task="task-003", type=DependencyType.RELATED),
                    ],
                ),
                make_task("task-001"),
                make_task("task-003"),
            ]
        )

        assert "needs" not in text

    def test_a_parent_is_named_with_its_title(self) -> None:
        text = render(
            [
                make_task("task-100", title="The umbrella"),
                make_task("task-002", parent="task-100"),
            ]
        )

        assert "part of task-100 (The umbrella)" in text


class TestProjectionBoundary:
    def test_the_published_fields_are_the_title_and_the_summary(self) -> None:
        task = make_task("task-001", title="A title", summary="A summary.")

        assert published_fields(task) == [("title", "A title"), ("spec.summary", "A summary.")]

    def test_nothing_outside_the_published_fields_reaches_the_file(self) -> None:
        """The record half a roadmap must not carry, each planted with its own marker.

        This is the test that would catch somebody adding "just the ball prompt" or "just
        the branch" to the entry -- each of which is a sentence written for one agent to
        read, on a page the whole internet can read.
        """
        task = make_task("task-001")
        task = task.model_copy(
            update={
                "ball_prompt": "BALLPROMPT",
                "branches": [Branch(name="BRANCHNAME")],
                "assignment": Assignment(owner="OWNERNAME"),
                "log": [
                    LogEntry(
                        id=1, ts=NOW, actor="OWNERNAME", type=LogEntryType.NOTE, body="LOGBODY"
                    )
                ],
                "spec": Spec(
                    summary="Summary.", description="DESCRIPTION", constraints="CONSTRAINT"
                ),
            }
        )
        text = render([task])

        for planted in (
            "BALLPROMPT",
            "BRANCHNAME",
            "OWNERNAME",
            "LOGBODY",
            "DESCRIPTION",
            "CONSTRAINT",
        ):
            assert planted not in text

    def test_the_file_carries_no_clock(self) -> None:
        """No generated-at stamp, and no record timestamp either.

        A stamp would make the file differ from its own regeneration the instant it was
        written, so the freshness check could never pass and would be switched off.
        """
        first = render([make_task("task-001")])
        later = make_task("task-001").model_copy(
            update={"updated": datetime(2027, 1, 1, tzinfo=timezone.utc)}
        )

        assert render([later]) == first
        assert "2026" not in first
        assert "2027" not in render([later])

    def test_an_empty_backlog_says_so_rather_than_rendering_nothing(self) -> None:
        assert "No task is ready or in progress." in render([])


class TestLeaks:
    @pytest.mark.parametrize(
        ("text", "what"),
        [
            (r"Fix the path under C:\Users\someone\projects", "a Windows home directory"),
            ("Fix the path under /home/someone/projects", "a Linux home directory"),
            ("Fix the path under /Users/someone/projects", "a macOS home directory"),
            ("Ask someone@example.com about it", "an email address"),
            ("Reachable at agentjobs.tailnet-name.ts.net", "a tailnet hostname"),
        ],
    )
    def test_a_personal_or_machine_local_marker_refuses_the_render(
        self, text: str, what: str
    ) -> None:
        """A refusal, not a redaction: a published page cannot be recalled by fixing it."""
        with pytest.raises(RoadmapLeakError) as caught:
            render([make_task("task-001", summary=text)])

        assert [leak.what for leak in caught.value.leaks] == [what]
        assert caught.value.leaks[0].task_id == "task-001"
        assert caught.value.leaks[0].field == "spec.summary"

    def test_a_marker_in_a_title_is_caught_too(self) -> None:
        found = leaks([make_task("task-001", title="Ask someone@example.com")])

        assert [leak.field for leak in found] == ["title"]

    def test_every_leak_is_reported_not_only_the_first(self) -> None:
        """The repair is to task records, and one finding per round-trip is the cost."""
        found = leaks(
            [
                make_task("task-001", summary="Ask someone@example.com"),
                make_task("task-002", summary="Under /home/someone/x"),
            ]
        )

        assert sorted(leak.task_id for leak in found) == ["task-001", "task-002"]

    def test_a_draft_is_not_scanned_because_it_is_not_published(self) -> None:
        """Scanning what is not published would block the gate over nothing."""
        assert (
            render(
                [
                    make_task("task-001"),
                    make_task(
                        "task-002",
                        lifecycle=Lifecycle.DRAFT,
                        summary="Under /home/someone/x",
                    ),
                ]
            )
            is not None
        )

    def test_an_ordinary_project_path_is_not_a_leak(self) -> None:
        """Narrow on purpose: a check that fires on what people defend gets disabled."""
        assert leaks([make_task("task-001", summary="See C:/projects/agentjobs/README.md")]) == []

    def test_a_leak_renders_as_one_actionable_line(self) -> None:
        leak = Leak(task_id="task-001", field="title", what="an email address", matched="a@b.co")

        assert leak.render() == "task-001 title contains an email address: a@b.co"
