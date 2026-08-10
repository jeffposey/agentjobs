"""Actors are resolved from config, so review actions name a person.

The bug this closes: all three GUI review buttons hardcoded ``user: 'human'``, so every
approval, change request and rejection was logged with the literal string "human". The
record showed that a person acted and never which one -- and the log is append-only, so
those entries can never be attributed later.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agentjobs.actors import (
    MULTIPLE,
    UNCONFIGURED,
    human_identity,
    UnknownActorError,
    default_user,
    humans,
    load_actors,
    validate_actor,
)

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


class TestExactlyOneHumanIsSupported:
    """Several people need account management, which is task-066, not a config shape."""

    TWO = {
        "actors": [
            {"name": "jeffposey", "kind": "human"},
            {"name": "sam", "kind": "human"},
        ]
    }

    def test_two_humans_is_refused_rather_than_guessed(self) -> None:
        identity = human_identity(self.TWO)

        assert not identity.ok
        assert identity.problem == MULTIPLE
        assert identity.user is None

    def test_a_stated_default_does_not_rescue_a_multi_human_config(self) -> None:
        # Tempting to let default_user disambiguate, but that is half-support: the
        # server still cannot tell who is at the keyboard, so it would confidently
        # attribute one person's approval to whoever config listed first.
        identity = human_identity({**self.TWO, "default_user": "sam"})

        assert not identity.ok
        assert identity.problem == MULTIPLE

    def test_the_message_names_the_people_and_the_fix(self) -> None:
        detail = human_identity(self.TWO).detail

        assert "jeffposey, sam" in detail
        assert "kind: human" in detail

    def test_an_unconfigured_project_is_a_different_problem(self) -> None:
        # Two failures needing different guidance: add yourself vs remove someone.
        identity = human_identity(LEGACY)

        assert identity.problem == UNCONFIGURED
        assert "Add an entry" in identity.detail

    def test_one_human_resolves_cleanly(self) -> None:
        identity = human_identity(UNIFIED)

        assert identity.ok
        assert identity.user == "jeffposey"
        assert identity.problem is None


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
