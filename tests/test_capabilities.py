"""The capability table itself: what each kind of principal holds, and actor agreement.

No transport here. :mod:`agentjobs.capabilities` takes a resolution and a config and
answers with a denial or nothing, so the rule can be argued with directly; the HTTP
wiring is exercised in ``tests/test_authorization.py`` and the end-to-end refusals a real
run gets in ``tests/test_run_authorization.py``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agentjobs.capabilities import (
    ACTOR_MISMATCH,
    CAPABILITY_DENIED,
    Capability,
    GRANTS,
    IDENTITY_UNRESOLVED,
    OWN_TASK_ONLY,
    WRONG_RUN,
    WRONG_TASK,
    actor_disagreement,
    authorize,
    granted,
)
from agentjobs.principals import (
    Principal,
    PrincipalKind,
    PrincipalSource,
    Problem,
    Resolution,
)
from agentjobs.projects import HOME_ENV


def owner() -> Resolution:
    return Resolution(
        principal=Principal(kind=PrincipalKind.OWNER, source=PrincipalSource.LOOPBACK)
    )


def tailnet(login: str = "jeff@example.com") -> Resolution:
    return Resolution(
        principal=Principal(
            kind=PrincipalKind.TAILNET,
            source=PrincipalSource.PROVEN_HEADER,
            login=login,
        )
    )


def run(
    *, run_id: str = "run_deadbeef", task_id: str = "task-001", agent: str = "claude"
) -> Resolution:
    return Resolution(
        principal=Principal(
            kind=PrincipalKind.RUN,
            source=PrincipalSource.RUN_CREDENTIAL,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent or None,
        )
    )


CONFIG: Dict[str, Any] = {
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "Sam", "kind": "human"},
        {"name": "claude", "kind": "agent"},
        {"name": "codex", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}
"""A project with two people and two agents -- the shape task-330 made legal, and the
one every interesting disagreement needs."""


@pytest.fixture(autouse=True)
def a_machine_that_knows_its_owner(tmp_path, monkeypatch) -> None:
    """Name the person at this machine, the way ``identities.yaml`` does.

    Without it, ``human_identity`` is right to refuse every owner request against
    :data:`CONFIG`: two people can act, nothing proved which one is asking, and task-330
    made guessing between them an error rather than a default. Writing the real file
    rather than injecting a registry keeps these tests on the path the server takes.
    """
    home = tmp_path / "agentjobs-home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    (home / "identities.yaml").write_text(
        "\n".join(
            [
                "owner: Jeff Posey",
                "identities:",
                "  - login: jeff@example.com",
                "    actor: Jeff Posey",
                "",
            ]
        ),
        encoding="utf-8",
    )


class TestTheTable:
    """The grants themselves, before anything consults them."""

    def test_every_kind_has_a_row(self) -> None:
        """A kind added without a decision must fail here, not default to something."""
        assert set(GRANTS) == set(PrincipalKind)

    @pytest.mark.parametrize("resolution", [owner(), tailnet()], ids=["owner", "tailnet"])
    def test_a_human_holds_everything(self, resolution: Resolution) -> None:
        """The task's binding constraint: no human loses a capability they have today."""
        principal = resolution.principal
        assert principal is not None
        assert granted(principal) == frozenset(Capability)

    def test_the_two_human_kinds_are_identical(self) -> None:
        """Owner and tailnet differ in how identity was proved, not in what they may do."""
        assert GRANTS[PrincipalKind.OWNER] == GRANTS[PrincipalKind.TAILNET]

    @pytest.mark.parametrize(
        "capability",
        [
            Capability.TASK_REVIEW,
            Capability.DISPATCH,
            Capability.DISPATCH_ADMIN,
            Capability.PROJECT_ADMIN,
            Capability.QUEUE_ADMIN,
            Capability.WEBHOOK_ADMIN,
        ],
    )
    def test_a_run_holds_none_of_the_human_acts(self, capability: Capability) -> None:
        """Every item on the spec's "may not" list, asserted one at a time."""
        assert capability not in GRANTS[PrincipalKind.RUN]

    def test_the_scoped_set_is_a_subset_of_what_a_run_holds(self) -> None:
        """Scoping something a run cannot do at all would be a rule nothing reaches."""
        assert OWN_TASK_ONLY <= GRANTS[PrincipalKind.RUN]


class TestAuthorize:
    """Turning a resolution and a capability into a denial, or into nothing."""

    def test_an_owner_is_refused_nothing(self) -> None:
        for capability in Capability:
            assert authorize(owner(), capability, task_id="task-999") is None

    def test_a_run_may_work_its_own_task(self) -> None:
        for capability in sorted(OWN_TASK_ONLY, key=lambda item: item.value):
            assert authorize(run(), capability, task_id="task-001") is None

    def test_a_run_may_not_touch_another_task(self) -> None:
        """The clause that matters as much as the list: scoped, not merely small."""
        denial = authorize(run(), Capability.TASK_VERB, task_id="task-002")
        assert denial is not None
        assert denial.code == WRONG_TASK
        assert "task-001" in denial.detail and "task-002" in denial.detail

    def test_a_run_may_not_approve_a_review(self) -> None:
        denial = authorize(run(), Capability.TASK_REVIEW, task_id="task-001")
        assert denial is not None
        assert denial.code == CAPABILITY_DENIED
        assert "run_deadbeef" in denial.detail

    def test_a_run_may_create_a_task_it_does_not_own(self) -> None:
        """Creating is deliberately unscoped: a new task is nobody's yet."""
        assert authorize(run(), Capability.TASK_CREATE) is None

    def test_a_run_reads_its_own_transcript_and_no_other(self) -> None:
        assert authorize(run(), Capability.RUN_OUTPUT, run_id="run_deadbeef") is None
        denial = authorize(run(), Capability.RUN_OUTPUT, run_id="run_00000000")
        assert denial is not None
        assert denial.code == WRONG_RUN

    def test_a_human_reading_a_transcript_is_not_scoped(self) -> None:
        """ac-5: identity-gated, not denied. The dashboard's output panel is a human."""
        assert authorize(owner(), Capability.RUN_OUTPUT, run_id="run_00000000") is None

    def test_no_principal_is_refused_and_says_why(self) -> None:
        """The problem code survives into the denial, so a caller can tell which it was."""
        unresolved = Resolution(
            problem=Problem.UNVERIFIED_RUN_CREDENTIAL,
            detail="A run credential was presented and did not verify.",
        )
        denial = authorize(unresolved, Capability.TASK_VERB, task_id="task-001")
        assert denial is not None
        assert denial.code == Problem.UNVERIFIED_RUN_CREDENTIAL.value
        assert "did not verify" in denial.detail


class TestActorAgreement:
    """The body field survives; it must agree."""

    def test_a_human_may_write_as_themselves(self) -> None:
        assert actor_disagreement(CONFIG, owner(), "Jeff Posey") is None

    def test_a_human_may_not_write_as_another_person(self) -> None:
        denial = actor_disagreement(CONFIG, owner(), "Sam")
        assert denial is not None
        assert denial.code == ACTOR_MISMATCH
        # ac-2: the message names both halves, because a misconfigured mapping is the
        # likeliest cause and "forbidden" alone would send somebody debugging.
        assert "'Sam'" in denial.detail
        assert "'Jeff Posey'" in denial.detail

    def test_a_human_may_write_as_an_agent(self) -> None:
        """The CLI and MCP run as the person here and attribute their writes to the tool."""
        assert actor_disagreement(CONFIG, owner(), "claude") is None

    def test_a_review_may_not_be_attributed_to_an_agent(self) -> None:
        """`require_human` is what the review loop adds, and this is why it exists."""
        denial = actor_disagreement(CONFIG, owner(), "claude", field="user", require_human=True)
        assert denial is not None
        assert denial.code == ACTOR_MISMATCH

    def test_a_run_may_write_as_the_agent_it_was_dispatched_as(self) -> None:
        assert actor_disagreement(CONFIG, run(agent="claude"), "claude") is None

    def test_a_run_may_never_write_as_a_person(self) -> None:
        """The impersonation the epic exists to close, checked at the field."""
        denial = actor_disagreement(CONFIG, run(), "Jeff Posey")
        assert denial is not None
        assert denial.code == ACTOR_MISMATCH
        assert "person" in denial.detail

    def test_a_run_may_not_write_as_another_agent(self) -> None:
        denial = actor_disagreement(CONFIG, run(agent="claude"), "codex")
        assert denial is not None
        assert denial.code == ACTOR_MISMATCH
        assert "'codex'" in denial.detail and "'claude'" in denial.detail

    def test_a_run_whose_agent_the_ledger_forgot_may_still_write_as_an_agent(self) -> None:
        """Unknown is not "any": the human claim is still refused just below."""
        assert actor_disagreement(CONFIG, run(agent=""), "codex") is None
        assert actor_disagreement(CONFIG, run(agent=""), "Jeff Posey") is not None

    def test_an_unconfigured_project_has_nothing_to_disagree_with(self) -> None:
        """A fresh `agentjobs init` names no actors; refusing there would be unusable."""
        assert actor_disagreement({}, owner(), "whoever") is None

    def test_an_omitted_field_is_not_a_claim(self) -> None:
        assert actor_disagreement(CONFIG, owner(), "") is None

    def test_a_proven_login_is_held_to_the_person_it_maps_to(self) -> None:
        """The multi-person case the epic exists for: each is held to their own id."""
        assert actor_disagreement(CONFIG, tailnet(), "Jeff Posey") is None
        denial = actor_disagreement(CONFIG, tailnet(), "Sam")
        assert denial is not None
        assert denial.code == ACTOR_MISMATCH

    def test_an_unresolvable_identity_is_reported_as_a_config_problem(
        self, tmp_path, monkeypatch
    ) -> None:
        """Two people, no proven login and no machine owner: the ambiguity task-330 names.

        400 rather than 403 at the HTTP layer, because this is not a refusal of the
        caller: it says the machine cannot work out who a legitimate caller is, and the
        fix is a line in a file rather than a different request.
        """
        home = tmp_path / "nameless-machine"
        home.mkdir()
        monkeypatch.setenv(HOME_ENV, str(home))
        denial = actor_disagreement(CONFIG, owner(), "Jeff Posey", require_human=True)
        assert denial is not None
        assert denial.code == IDENTITY_UNRESOLVED
        assert "cannot tell which" in denial.detail

    def test_a_claim_with_no_principal_behind_it_is_refused(self) -> None:
        denial = actor_disagreement(
            CONFIG, Resolution(problem=Problem.NO_PROVEN_IDENTITY), "claude"
        )
        assert denial is not None
        assert denial.code == Problem.NO_PROVEN_IDENTITY.value
