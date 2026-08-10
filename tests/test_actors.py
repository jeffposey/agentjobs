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
    UnknownActorError,
    default_user,
    humans,
    load_actors,
    validate_actor,
)

UNIFIED: Dict[str, Any] = {
    "actors": [
        {"name": "jeff", "kind": "human", "display_name": "Jeff Posey"},
        {"name": "claude", "kind": "agent", "display_name": "Claude"},
    ],
    "default_user": "jeff",
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

        assert set(actors) == {"jeff", "claude"}
        assert actors["jeff"].is_human
        assert not actors["claude"].is_human

    def test_a_legacy_agents_list_still_loads_as_agents(self) -> None:
        # Existing projects have `agents:` and must keep working untouched; requiring a
        # config edit before the app runs would be a migration disguised as a feature.
        actors = load_actors(LEGACY)

        assert set(actors) == {"claude", "codex"}
        assert not any(actor.is_human for actor in actors.values())

    def test_both_lists_merge_so_a_project_can_adopt_actors_gradually(self) -> None:
        merged = load_actors({**LEGACY, "actors": [{"name": "jeff", "kind": "human"}]})

        assert set(merged) == {"claude", "codex", "jeff"}

    def test_actors_wins_for_an_id_defined_in_both(self) -> None:
        both = load_actors(
            {
                "agents": [{"name": "claude", "display_name": "stale"}],
                "actors": [{"name": "claude", "kind": "agent", "display_name": "current"}],
            }
        )

        assert both["claude"].display_name == "current"

    def test_a_bare_string_entry_is_accepted(self) -> None:
        assert load_actors({"actors": ["jeff"]})["jeff"].is_human

    def test_an_entry_with_no_name_is_skipped_rather_than_crashing(self) -> None:
        assert load_actors({"actors": [{"display_name": "nameless"}, {"name": "jeff"}]}).keys() == {
            "jeff"
        }


class TestTheDefaultUser:
    def test_default_user_is_honoured(self) -> None:
        assert default_user(UNIFIED) == "jeff"

    def test_a_lone_human_needs_no_explicit_default(self) -> None:
        assert default_user({"actors": [{"name": "jeff", "kind": "human"}]}) == "jeff"

    def test_it_refuses_to_guess_between_two_people(self) -> None:
        # Attributing one person's approval to another is worse than attributing it to
        # nobody, so an ambiguous config yields None and the GUI disables the actions.
        config = {
            "actors": [
                {"name": "jeff", "kind": "human"},
                {"name": "sam", "kind": "human"},
            ]
        }

        assert default_user(config) is None

    def test_an_agents_only_project_has_no_default_user(self) -> None:
        assert default_user(LEGACY) is None
        assert humans(LEGACY) == []


class TestValidation:
    def test_a_configured_actor_passes(self) -> None:
        assert validate_actor(UNIFIED, "jeff") == "jeff"

    def test_an_unknown_actor_is_refused_and_the_message_lists_the_real_ones(self) -> None:
        # D2: an unrecognised id written into an append-only log is a silent no-op that
        # survives forever.
        with pytest.raises(UnknownActorError) as caught:
            validate_actor(UNIFIED, "jef")

        assert "jef" in str(caught.value)
        assert "claude, jeff" in str(caught.value)

    def test_the_old_hardcoded_value_is_now_refused(self) -> None:
        # The literal string every review action used to be logged as.
        with pytest.raises(UnknownActorError):
            validate_actor(UNIFIED, "human")

    def test_anything_is_accepted_when_config_defines_no_actors(self) -> None:
        # A fresh `agentjobs init` that has not been edited must still be usable;
        # validating against an empty vocabulary would reject every action.
        assert validate_actor({}, "anyone") == "anyone"
