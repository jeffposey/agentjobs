"""The machine-level login-to-actor map, and the file a second person edits.

Split from ``test_actors.py`` because the two answer different questions. That module
asks what AgentJobs does with a mapping; this one asks whether the file parses the way
the refusal message says it does -- which matters more than it sounds, since that
message is the only instruction a new person gets.
"""

from __future__ import annotations

from pathlib import Path

from agentjobs.identities import (
    IDENTITIES_FILENAME,
    IdentityRegistry,
    identities_path,
    unmapped_login_help,
)
from agentjobs.projects import HOME_ENV


def write(home: Path, body: str) -> IdentityRegistry:
    """Write raw YAML into the registry home and read it back."""
    home.mkdir(parents=True, exist_ok=True)
    (home / IDENTITIES_FILENAME).write_text(body, encoding="utf-8")
    return IdentityRegistry(home=home)


class TestWhereItLives:
    def test_it_sits_beside_the_project_registry(self, tmp_path: Path) -> None:
        # Both are machine-level and disposable: a list of what this machine has, which
        # is why neither belongs in a repository that gets cloned.
        assert identities_path(tmp_path) == tmp_path / "identities.yaml"

    def test_it_honours_the_registry_home_override(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(HOME_ENV, str(tmp_path))

        assert IdentityRegistry().path == tmp_path / IDENTITIES_FILENAME

    def test_an_absent_file_maps_nothing_rather_than_failing(self, tmp_path: Path) -> None:
        # The overwhelmingly common case: one person, no file, nothing to configure.
        registry = IdentityRegistry(home=tmp_path)

        assert registry.resolve("anyone@example.com") is None
        assert registry.owner is None
        assert registry.logins() == []


class TestTheEntryShapeTheMessagePrints:
    """The refusal tells a new person what to write. This is that, round-tripped."""

    def test_the_documented_shape_parses(self, tmp_path: Path) -> None:
        registry = write(
            tmp_path,
            "identities:\n  - login: jeff@example.com\n    actor: jeffposey\n",
        )

        mapped = registry.resolve("jeff@example.com")
        assert mapped is not None
        assert mapped.actor_id == "jeffposey"

    def test_actor_id_is_accepted_as_a_synonym_for_actor(self, tmp_path: Path) -> None:
        registry = write(
            tmp_path,
            "identities:\n  - login: jeff@example.com\n    actor_id: jeffposey\n",
        )

        assert registry.resolve("jeff@example.com") is not None

    def test_surrounding_whitespace_does_not_break_a_match(self, tmp_path: Path) -> None:
        registry = write(
            tmp_path,
            'identities:\n  - login: "  jeff@example.com  "\n    actor: jeffposey\n',
        )

        assert registry.resolve("jeff@example.com") is not None

    def test_the_owner_line_is_read(self, tmp_path: Path) -> None:
        assert write(tmp_path, "owner: jeffposey\n").owner == "jeffposey"


class TestABadFileDoesNotLockAnybodyOut:
    """Hand-edited YAML fails in hand-edited ways, and it must fail small."""

    def test_unparseable_yaml_maps_nothing_instead_of_raising(self, tmp_path: Path) -> None:
        # A raise here reaches the caller as a 500 on every page, for a file that is
        # only consulted to *add* people. Mapping nothing degrades to the single-person
        # behaviour, which is a refusal the reader can act on.
        registry = write(tmp_path, "identities: [unclosed\n")

        assert registry.resolve("jeff@example.com") is None

    def test_a_scalar_document_maps_nothing(self, tmp_path: Path) -> None:
        assert write(tmp_path, "just a string\n").resolve("jeff@example.com") is None

    def test_an_entry_missing_its_actor_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        registry = write(
            tmp_path,
            "identities:\n  - login: broken@example.com\n"
            "  - login: jeff@example.com\n    actor: jeffposey\n",
        )

        assert registry.logins() == ["jeff@example.com"]

    def test_a_non_mapping_entry_is_skipped(self, tmp_path: Path) -> None:
        registry = write(
            tmp_path,
            "identities:\n  - jeff@example.com\n  - login: sam@example.com\n    actor: sam\n",
        )

        assert registry.actor_ids() == ["sam"]


class TestTheOnboardingMessage:
    """It is the entire experience of being a second person on this machine."""

    def test_it_names_the_login_the_file_and_a_copyable_entry(self, tmp_path: Path) -> None:
        message = unmapped_login_help("sam@example.com", home=tmp_path)

        assert "sam@example.com" in message
        assert str(tmp_path / IDENTITIES_FILENAME) in message
        assert "identities:" in message
        assert "- login: sam@example.com" in message

    def test_the_entry_it_prints_is_the_entry_that_parses(self, tmp_path: Path) -> None:
        # The one test that makes this message trustworthy rather than merely helpful:
        # the shape it tells the reader to write is fed straight back through the
        # parser, with only the placeholder actor id filled in.
        printed = unmapped_login_help("sam@example.com", home=tmp_path)
        block = printed.split("identities:", 1)[1]
        yaml_body = "identities:" + block.replace(
            "<an id from 'actors:' in this project's .agentjobs/config.yaml>", "sam"
        )

        registry = write(tmp_path, yaml_body)
        mapped = registry.resolve("sam@example.com")

        assert mapped is not None
        assert mapped.actor_id == "sam"
