"""Actors are resolved from config, so review actions name a person.

The bug this closes: all three GUI review buttons hardcoded ``user: 'human'``, so every
approval, change request and rejection was logged with the literal string "human". The
record showed that a person acted and never which one -- and the log is append-only, so
those entries can never be attributed later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from agentjobs.actors import (
    AGENT,
    AMBIGUOUS,
    HUMAN,
    NOT_A_PERSON,
    RETIRED,
    UNCONFIGURED,
    UNKNOWN_ACTOR,
    UNMAPPED,
    actor_kinds,
    active_humans,
    human_identity,
    RetiredActorError,
    UnknownActorError,
    default_user,
    humans,
    load_actors,
    validate_actor,
)
from agentjobs.identities import IDENTITIES_FILENAME, IdentityRegistry
from agentjobs.principals import Principal, PrincipalKind, PrincipalSource


def write_identities(
    home: Path,
    *,
    identities: Optional[List[Dict[str, Any]]] = None,
    owner: Optional[str] = None,
) -> IdentityRegistry:
    """Write a machine-level identity map into ``home`` and read it back.

    Written to a real file rather than stubbed, because the file is the interface: a
    second person's whole onboarding is editing it, and a test that bypasses the parse
    would not notice the shape of the entry the refusal message tells them to write.
    """
    payload: Dict[str, Any] = {}
    if owner is not None:
        payload["owner"] = owner
    if identities is not None:
        payload["identities"] = identities
    home.mkdir(parents=True, exist_ok=True)
    (home / IDENTITIES_FILENAME).write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return IdentityRegistry(home=home)


def tailnet(login: str) -> Principal:
    """A caller the front door proved, which is the only kind carrying a login."""
    return Principal(kind=PrincipalKind.TAILNET, source=PrincipalSource.PROVEN_HEADER, login=login)


UNIFIED: Dict[str, Any] = {
    "actors": [
        {"name": "jeffposey", "kind": "human", "display_name": "Jeff Posey"},
        {"name": "claude", "kind": "agent", "display_name": "Claude"},
    ],
    "default_user": "jeffposey",
}

LEGACY: Dict[str, Any] = {
    "agents": [
        {"name": "claude", "display_name": "Claude"},
        {"name": "codex", "display_name": "Codex"},
    ]
}


class TestLoadingTheVocabulary:
    def test_reads_the_unified_actors_list_with_kinds(self) -> None:
        actors = load_actors(UNIFIED)

        assert set(actors) == {"jeffposey", "claude"}
        assert actors["jeffposey"].is_human
        assert not actors["claude"].is_human

    def test_a_legacy_agents_list_still_loads_as_agents(self) -> None:
        # Existing projects have `agents:` and must keep working untouched; requiring a
        # config edit before the app runs would be a migration disguised as a feature.
        actors = load_actors(LEGACY)

        assert set(actors) == {"claude", "codex"}
        assert not any(actor.is_human for actor in actors.values())

    def test_both_lists_merge_so_a_project_can_adopt_actors_gradually(self) -> None:
        merged = load_actors({**LEGACY, "actors": [{"name": "jeffposey", "kind": "human"}]})

        assert set(merged) == {"claude", "codex", "jeffposey"}

    def test_actors_wins_for_an_id_defined_in_both(self) -> None:
        both = load_actors(
            {
                "agents": [{"name": "claude", "display_name": "stale"}],
                "actors": [{"name": "claude", "kind": "agent", "display_name": "current"}],
            }
        )

        assert both["claude"].display_name == "current"

    def test_a_bare_string_entry_is_accepted(self) -> None:
        assert load_actors({"actors": ["jeffposey"]})["jeffposey"].is_human

    def test_an_entry_with_no_name_is_skipped_rather_than_crashing(self) -> None:
        assert load_actors(
            {"actors": [{"display_name": "nameless"}, {"name": "jeffposey"}]}
        ).keys() == {"jeffposey"}


class TestTheDefaultUser:
    def test_default_user_is_honoured(self) -> None:
        assert default_user(UNIFIED) == "jeffposey"

    def test_a_lone_human_needs_no_explicit_default(self) -> None:
        assert default_user({"actors": [{"name": "jeffposey", "kind": "human"}]}) == "jeffposey"

    def test_an_agents_only_project_has_no_default_user(self) -> None:
        assert default_user(LEGACY) is None
        assert humans(LEGACY) == []


class TestSeveralPeople:
    """A config naming two people is accepted, and each request names one of them.

    This is the refusal task-064 installed and task-330 lifts. The old test in this
    place asserted that two ``kind: human`` entries were refused outright; the reason it
    gave -- "the server still cannot tell who is at the keyboard" -- is what changed,
    not the principle. The principle is intact below: a request that still cannot say
    who is asking is still refused, and nothing is guessed.
    """

    TWO = {
        "actors": [
            {"name": "jeffposey", "kind": "human"},
            {"name": "sam", "kind": "human"},
        ]
    }

    def test_two_humans_are_both_loaded_and_both_active(self) -> None:
        assert {person.id for person in humans(self.TWO)} == {"jeffposey", "sam"}
        assert {person.id for person in active_humans(self.TWO)} == {"jeffposey", "sam"}

    def test_each_proven_login_resolves_to_its_own_actor(self, tmp_path: Path) -> None:
        # ac-1: attribution is per request, not per project. The same config answers
        # differently for two callers, which is the whole point of the task.
        registry = write_identities(
            tmp_path,
            identities=[
                {"login": "jeff@example.com", "actor": "jeffposey"},
                {"login": "sam@example.com", "actor": "sam"},
            ],
        )

        jeff = human_identity(self.TWO, tailnet("jeff@example.com"), identities=registry)
        sam = human_identity(self.TWO, tailnet("sam@example.com"), identities=registry)

        assert (jeff.user, sam.user) == ("jeffposey", "sam")

    def test_a_login_matches_regardless_of_case(self, tmp_path: Path) -> None:
        # A refusal caused by capitalisation is indistinguishable from a refusal caused
        # by not being registered, and only one of them has a fix the reader can find.
        registry = write_identities(
            tmp_path, identities=[{"login": "Jeff@Example.com", "actor": "jeffposey"}]
        )

        assert human_identity(self.TWO, tailnet("jeff@EXAMPLE.com"), identities=registry).user == (
            "jeffposey"
        )

    def test_no_proven_identity_is_still_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        identity = human_identity(self.TWO, identities=write_identities(tmp_path))

        assert not identity.ok
        assert identity.problem == AMBIGUOUS
        assert "jeffposey, sam" in identity.detail

    def test_a_stated_default_still_does_not_rescue_an_unidentified_request(
        self, tmp_path: Path
    ) -> None:
        # Unchanged from task-064, and load-bearing: project config is committed and
        # travels with a clone, so it cannot say who is at *this* keyboard. Honouring
        # it here would record one person's approval as the other's.
        identity = human_identity(
            {**self.TWO, "default_user": "sam"}, identities=write_identities(tmp_path)
        )

        assert not identity.ok
        assert identity.problem == AMBIGUOUS

    def test_the_refusal_names_the_two_ways_out(self, tmp_path: Path) -> None:
        detail = human_identity(self.TWO, identities=write_identities(tmp_path)).detail

        assert "front door" in detail
        assert "owner:" in detail
        assert "identities.yaml" in detail

    def test_the_machine_owner_answers_a_request_with_no_login(self, tmp_path: Path) -> None:
        # The person at the keyboard presents no login to look up, so the machine says
        # which of the configured people they are -- machine-level, because that is a
        # fact about this machine and not about the repository.
        registry = write_identities(tmp_path, owner="sam")

        assert human_identity(self.TWO, identities=registry).user == "sam"

    def test_a_proven_login_beats_the_machine_owner(self, tmp_path: Path) -> None:
        registry = write_identities(
            tmp_path, owner="sam", identities=[{"login": "jeff@example.com", "actor": "jeffposey"}]
        )

        assert (
            human_identity(self.TWO, tailnet("jeff@example.com"), identities=registry).user
            == "jeffposey"
        )

    def test_an_unconfigured_project_is_a_different_problem(self) -> None:
        # Two failures needing different guidance: add yourself vs prove who you are.
        identity = human_identity(LEGACY)

        assert identity.problem == UNCONFIGURED
        assert "Add an entry" in identity.detail

    def test_one_human_resolves_cleanly(self) -> None:
        identity = human_identity(UNIFIED)

        assert identity.ok
        assert identity.user == "jeffposey"
        assert identity.problem is None


class TestAnUnmappedLoginIsRefused:
    """ac-3. The refusal is the whole onboarding experience for a second person."""

    def test_it_does_not_fall_back_to_default_user(self, tmp_path: Path) -> None:
        # The defect task-064 removed. A proven login that maps to nothing must not be
        # recorded as whoever config happens to name, however convenient that is.
        identity = human_identity(
            UNIFIED, tailnet("stranger@example.com"), identities=write_identities(tmp_path)
        )

        assert not identity.ok
        assert identity.user is None
        assert identity.user != "jeffposey"
        assert identity.problem == UNMAPPED

    def test_the_message_names_the_login_the_file_and_the_entry(self, tmp_path: Path) -> None:
        detail = human_identity(
            UNIFIED, tailnet("stranger@example.com"), identities=write_identities(tmp_path)
        ).detail

        assert "stranger@example.com" in detail
        assert str(tmp_path / IDENTITIES_FILENAME) in detail
        assert "identities:" in detail
        assert "- login: stranger@example.com" in detail
        assert "actor:" in detail

    def test_a_mapping_to_an_id_this_project_does_not_define_is_refused(
        self, tmp_path: Path
    ) -> None:
        registry = write_identities(
            tmp_path, identities=[{"login": "sam@example.com", "actor": "sam"}]
        )

        identity = human_identity(UNIFIED, tailnet("sam@example.com"), identities=registry)

        assert identity.problem == UNKNOWN_ACTOR
        assert "sam" in identity.detail
        assert "claude, jeffposey" in identity.detail

    def test_a_malformed_entry_is_skipped_rather_than_locking_everyone_out(
        self, tmp_path: Path
    ) -> None:
        # The file is hand-edited. One person's typo must not take the dashboard away
        # from everybody else, and the login it fails to map gets the ordinary refusal.
        registry = write_identities(
            tmp_path,
            identities=[
                {"login": "broken@example.com"},
                {"login": "jeff@example.com", "actor": "jeffposey"},
            ],
        )

        assert human_identity(UNIFIED, tailnet("jeff@example.com"), identities=registry).user == (
            "jeffposey"
        )
        assert (
            human_identity(UNIFIED, tailnet("broken@example.com"), identities=registry).problem
            == UNMAPPED
        )


class TestARunIsNeverAPerson:
    def test_a_dispatched_run_resolves_to_no_human(self) -> None:
        identity = human_identity(
            UNIFIED,
            Principal(
                kind=PrincipalKind.RUN,
                source=PrincipalSource.RUN_CREDENTIAL,
                run_id="run_abc",
                task_id="task-330",
            ),
        )

        assert not identity.ok
        assert identity.problem == NOT_A_PERSON
        assert "run_abc" in identity.detail


class TestRetirement:
    """ac-4. A retired person stops acting; their history is untouched."""

    RETIRED_CONFIG = {
        "actors": [
            {"name": "jeffposey", "kind": "human"},
            {"name": "sam", "kind": "human", "retired": True},
            {"name": "claude", "kind": "agent"},
        ]
    }

    def test_a_retired_actor_cannot_be_written_as(self) -> None:
        with pytest.raises(RetiredActorError) as caught:
            validate_actor(self.RETIRED_CONFIG, "sam")

        assert "retired" in str(caught.value)

    def test_the_refusal_is_also_an_unknown_actor_error(self) -> None:
        # Deliberate: every caller that already turns an unwritable id into a 400 does
        # so for this one too, with its own message and no plumbing.
        with pytest.raises(UnknownActorError):
            validate_actor(self.RETIRED_CONFIG, "sam")

    def test_a_retired_login_is_refused_with_a_message_saying_so(self, tmp_path: Path) -> None:
        registry = write_identities(
            tmp_path, identities=[{"login": "sam@example.com", "actor": "sam"}]
        )

        identity = human_identity(
            self.RETIRED_CONFIG, tailnet("sam@example.com"), identities=registry
        )

        assert identity.problem == RETIRED
        assert "sam" in identity.detail

    def test_a_retired_default_user_is_refused_too(self, tmp_path: Path) -> None:
        identity = human_identity(
            {"actors": [{"name": "sam", "kind": "human", "retired": True}], "default_user": "sam"},
            identities=write_identities(tmp_path),
        )

        assert identity.problem == RETIRED

    def test_retirement_leaves_the_only_other_person_unambiguous(self, tmp_path: Path) -> None:
        # Two people configured, one retired: exactly one can act, so nothing is
        # ambiguous and the dashboard keeps working with no further configuration.
        assert (
            human_identity(self.RETIRED_CONFIG, identities=write_identities(tmp_path)).user
            == "jeffposey"
        )

    def test_a_log_written_before_the_retirement_still_resolves(self) -> None:
        # The constraint, stated as a test: the log is append-only, and an entry naming
        # a retired person must still render. Everything a renderer asks -- is this id
        # known, what kind wrote it, what is it called -- keeps answering.
        before_retirement = {"actor": "sam", "type": "note", "body": "Approved."}

        kinds = actor_kinds(self.RETIRED_CONFIG)
        known = load_actors(self.RETIRED_CONFIG)

        assert kinds[before_retirement["actor"]] == HUMAN
        assert known["sam"].display_name == "sam"
        assert known["sam"].is_human

    def test_a_retired_actor_is_not_offered_as_a_configured_one(self) -> None:
        # The message a refusal prints lists who you *may* write as. Naming somebody who
        # has retired would send the reader straight into a second refusal.
        with pytest.raises(UnknownActorError) as caught:
            validate_actor(self.RETIRED_CONFIG, "stranger")

        assert "claude, jeffposey" in str(caught.value)
        assert "sam" not in str(caught.value).split("Configured actors:")[1].split(".")[0]


class TestExistingSingleHumanConfigsAreUntouched:
    """ac-5. Nobody has to edit YAML because this landed."""

    def test_the_canonical_single_human_config_still_resolves(self) -> None:
        assert default_user(UNIFIED) == "jeffposey"

    def test_it_resolves_the_same_from_a_bare_loopback_request(self) -> None:
        owner = Principal(kind=PrincipalKind.OWNER, source=PrincipalSource.LOOPBACK)

        assert default_user(UNIFIED, owner) == "jeffposey"

    def test_a_lone_human_with_no_default_user_still_resolves(self) -> None:
        assert default_user({"actors": [{"name": "jeffposey", "kind": "human"}]}) == "jeffposey"

    def test_an_agents_only_project_still_has_no_default_user(self) -> None:
        assert default_user(LEGACY) is None

    def test_a_default_user_no_actors_list_names_still_resolves(self) -> None:
        # Installs exist whose `default_user` was never added to `actors:`. That config
        # works today -- `validate_actor` accepts any id where no actor is configured --
        # and refusing it here would be a migration disguised as a feature.
        assert default_user({"default_user": "jeffposey"}) == "jeffposey"


class TestIdsWithSpaces:
    """An actor id may contain spaces, so it can read as a person's name.

    The textbook objection is that an id is a reference and `display_name` exists for
    readability. The owner chose readability in the stored record, which is his to
    choose -- these tests exist so the choice is safe rather than merely honoured. An id
    travels through YAML, a URL query string and a CLI argument, and each has its own
    way of mangling a space.
    """

    SPACED: Dict[str, Any] = {
        "actors": [
            {"name": "Jeff Posey", "kind": "human", "display_name": "Jeff Posey"},
            {"name": "claude", "kind": "agent"},
        ],
        "default_user": "Jeff Posey",
    }

    def test_it_resolves_as_the_acting_user(self) -> None:
        identity = human_identity(self.SPACED)

        assert identity.ok
        assert identity.user == "Jeff Posey"

    def test_it_validates(self) -> None:
        assert validate_actor(self.SPACED, "Jeff Posey") == "Jeff Posey"

    def test_a_near_miss_is_still_refused(self) -> None:
        # Two spaces, not one. Exactly the typo a space-bearing id invites.
        with pytest.raises(UnknownActorError):
            validate_actor(self.SPACED, "Jeff  Posey")

    def test_it_survives_a_yaml_round_trip_unquoted(self) -> None:
        # safe_dump does not quote a bare string with an internal space, and safe_load
        # must read it back identically -- otherwise every task file holding the id
        # would drift on its next write.
        import yaml

        dumped = yaml.safe_dump({"actor": "Jeff Posey"}, allow_unicode=False)
        assert yaml.safe_load(dumped)["actor"] == "Jeff Posey"


class TestValidation:
    def test_a_configured_actor_passes(self) -> None:
        assert validate_actor(UNIFIED, "jeffposey") == "jeffposey"

    def test_an_unknown_actor_is_refused_and_the_message_lists_the_real_ones(self) -> None:
        # D2: an unrecognised id written into an append-only log is a silent no-op that
        # survives forever.
        with pytest.raises(UnknownActorError) as caught:
            validate_actor(UNIFIED, "jefposey")

        assert "jefposey" in str(caught.value)
        assert "claude, jeffposey" in str(caught.value)

    def test_the_old_hardcoded_value_is_now_refused(self) -> None:
        # The literal string every review action used to be logged as.
        with pytest.raises(UnknownActorError):
            validate_actor(UNIFIED, "human")

    def test_anything_is_accepted_when_config_defines_no_actors(self) -> None:
        # A fresh `agentjobs init` that has not been edited must still be usable;
        # validating against an empty vocabulary would reject every action.
        assert validate_actor({}, "anyone") == "anyone"


class TestActorKinds:
    """The id-to-kind mapping a reader of a log entry needs (task-217)."""

    def test_maps_every_configured_id_to_its_kind(self) -> None:
        assert actor_kinds(UNIFIED) == {
            "jeffposey": HUMAN,
            "claude": AGENT,
            "dispatcher": AGENT,
            "finisher": AGENT,
        }

    def test_a_legacy_agents_list_is_read_too(self) -> None:
        kinds = actor_kinds(LEGACY)

        assert kinds["claude"] == AGENT
        assert kinds["codex"] == AGENT

    def test_an_unconfigured_id_is_simply_absent(self) -> None:
        # Absent means unknown. A caller must not read it as either kind, which is why
        # nothing is invented here for it.
        assert "stranger" not in actor_kinds(UNIFIED)

    def test_the_reserved_ids_win_over_config(self) -> None:
        # Same rule as dispatch.guards.actor_kind, and for the same reason: a project
        # that called the dispatcher a human would otherwise have every entry AgentJobs
        # writes for itself read as a human act.
        claimed_human = {"actors": [{"name": "dispatcher", "kind": "human"}]}

        assert actor_kinds(claimed_human)["dispatcher"] == AGENT
