"""Which address a dispatched agent is told AgentJobs is on.

The bug these exist for (task-154) was not a wrong constant. It was a constant repeated
as a parameter default in the runner, the guards and the auto trigger, with the HTTP
endpoint passing nothing -- so the value every dispatched agent actually received was
decided by whichever default it fell through to, and no caller could tell. The tests
here pin the one resolver that replaced all of them, and its order of preference.

The consequence of getting it wrong is why this is covered at all: an agent that cannot
reach AgentJobs cannot write to AgentJobs, so it cannot report that it cannot reach
AgentJobs. The only symptom is a run that goes quiet.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentjobs.dispatch.address import (
    API_BASE_ENV,
    DEFAULT_API_BASE,
    api_base_from_server,
    configured_api_base,
    normalise_api_base,
    resolve_api_base,
)
from agentjobs.dispatch.config import CONFIG_FILENAME, DispatchConfigError, load_dispatch_config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway AgentJobs home, and no inherited address in the environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(API_BASE_ENV, raising=False)
    return home


def write_config(home: Path, **overrides: object) -> Path:
    config: dict = {
        "version": 1,
        "enabled": True,
        "runners": {"claude": {"argv": ["claude", "-p", "{prompt}"]}},
    }
    config.update(overrides)
    path = home / CONFIG_FILENAME
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


class TestTheAddressOfTheSocketThatAnswered:
    """The ASGI ``server`` scope entry is the listening socket, which is why it wins."""

    def test_it_is_the_bound_host_and_port(self) -> None:
        assert api_base_from_server("127.0.0.1", 8901) == "http://127.0.0.1:8901"

    def test_a_wildcard_bind_becomes_loopback(self) -> None:
        # 0.0.0.0 is a valid thing to bind and not a valid thing to connect to, and the
        # agent being handed this address is on this machine.
        assert api_base_from_server("0.0.0.0", 8876) == "http://127.0.0.1:8876"
        assert api_base_from_server("::", 8876) == "http://127.0.0.1:8876"
        assert api_base_from_server("", 8876) == "http://127.0.0.1:8876"

    def test_an_ipv6_literal_is_bracketed_so_the_url_parses(self) -> None:
        assert api_base_from_server("::1", 8876) == "http://[::1]:8876"

    def test_no_port_means_no_answer_rather_than_a_guess(self) -> None:
        # Some ASGI servers supply no `server` entry. Returning None hands the question
        # back to the configured sources instead of inventing a port.
        assert api_base_from_server("127.0.0.1", None) is None

    def test_the_scheme_is_always_http_even_behind_a_tls_proxy(self) -> None:
        """The socket speaks plain HTTP; TLS in front of it belongs to something else.

        This is the whole reason the socket is preferred to the ``Host`` header. The
        dashboard is published over a tailnet through a loopback proxy, so the request
        arrives claiming https and a public hostname while the agent that has to use the
        address is a local process. Reading the proxy's word for it would produce an
        https loopback address, which nothing serves.
        """
        resolved = api_base_from_server("127.0.0.1", 8876)

        assert resolved is not None and resolved.startswith("http://")


class TestConfiguredAddress:
    def test_the_environment_wins_over_the_file(self, isolated_home: Path, monkeypatch) -> None:
        write_config(isolated_home, api_base="http://localhost:8876")
        monkeypatch.setenv(API_BASE_ENV, "http://localhost:9001")

        assert configured_api_base() == "http://localhost:9001"

    def test_the_file_is_read_when_the_environment_is_silent(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://localhost:8876")

        assert configured_api_base() == "http://localhost:8876"

    def test_a_trailing_slash_is_trimmed_on_both_paths(
        self, isolated_home: Path, monkeypatch
    ) -> None:
        # Every consumer appends /api/... to this, so the two spellings have to mean the
        # same thing rather than producing a doubled slash in a URL an agent reports.
        write_config(isolated_home, api_base="http://localhost:8876/")
        assert configured_api_base() == "http://localhost:8876"

        monkeypatch.setenv(API_BASE_ENV, "  http://localhost:9001/  ")
        assert configured_api_base() == "http://localhost:9001"

    def test_no_config_and_no_environment_is_no_answer(self, isolated_home: Path) -> None:
        assert configured_api_base() is None

    def test_a_config_without_an_api_base_is_no_answer(self, isolated_home: Path) -> None:
        write_config(isolated_home)

        assert configured_api_base() is None

    def test_an_unreadable_config_defers_rather_than_raising(self, isolated_home: Path) -> None:
        """The gates refuse an unparseable config far better than a resolver could.

        Raising here would replace "your dispatch.yaml is invalid at line N" with an
        error about an address, which is not what is wrong.
        """
        (isolated_home / CONFIG_FILENAME).write_text("version: 99\n", encoding="utf-8")

        assert configured_api_base() is None
        with pytest.raises(DispatchConfigError):
            load_dispatch_config()


class TestResolutionOrder:
    def test_an_explicit_address_beats_everything_configured(
        self, isolated_home: Path, monkeypatch
    ) -> None:
        """Observed beats declared: the socket answering the call cannot be stale."""
        write_config(isolated_home, api_base="http://localhost:8876")
        monkeypatch.setenv(API_BASE_ENV, "http://localhost:9001")

        assert resolve_api_base("http://127.0.0.1:8901") == "http://127.0.0.1:8901"

    def test_nothing_explicit_falls_through_to_configuration(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://localhost:8876")

        assert resolve_api_base(None) == "http://localhost:8876"

    def test_a_blank_explicit_address_is_treated_as_no_answer(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://localhost:8876")

        assert resolve_api_base("   ") == "http://localhost:8876"

    def test_a_silent_machine_gets_the_documented_default(self, isolated_home: Path) -> None:
        assert resolve_api_base() == DEFAULT_API_BASE

    def test_the_default_lives_in_exactly_one_place(self) -> None:
        """The regression guard. The bug was three copies of this string, not its value.

        A default repeated at every level of a call chain is a default no caller can
        override reliably, because omitting the argument silently reinstates it. Across
        the dispatch package, this literal belongs to the resolver alone.
        """
        package = Path(__file__).resolve().parents[1] / "src" / "agentjobs" / "dispatch"
        offenders = [
            path.name
            for path in package.glob("*.py")
            if path.name != "address.py" and DEFAULT_API_BASE in path.read_text(encoding="utf-8")
        ]

        assert offenders == [], (
            f"{offenders} hardcode {DEFAULT_API_BASE}. Resolve through "
            "dispatch.address.resolve_api_base instead -- see task-154."
        )


class TestNormalisation:
    def test_it_trims_whitespace_and_trailing_slashes_only(self) -> None:
        assert normalise_api_base(" http://host:8876/// ") == "http://host:8876"

    def test_it_does_not_correct_an_address_someone_typed(self) -> None:
        """Rewriting is how a value ends up matching nothing in any file on the machine."""
        assert normalise_api_base("HTTP://Host:8876") == "HTTP://Host:8876"


class TestConfigSchema:
    def test_api_base_is_parsed_off_the_top_level(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://localhost:8876")

        config = load_dispatch_config()

        assert config is not None
        assert config.api_base == "http://localhost:8876"

    @pytest.mark.parametrize("bad", [8876, "", "   ", [], {}])
    def test_an_api_base_that_is_not_an_address_is_a_load_error(
        self, isolated_home: Path, bad: object
    ) -> None:
        write_config(isolated_home, api_base=bad)

        with pytest.raises(DispatchConfigError) as caught:
            load_dispatch_config()
        assert "api_base" in str(caught.value)


def test_the_environment_name_matches_the_family_it_belongs_to() -> None:
    assert API_BASE_ENV == "AGENTJOBS_API_BASE"
    assert os.environ.get(API_BASE_ENV) is None
