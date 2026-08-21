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

import http.server
import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest
import yaml

from agentjobs.dispatch.address import (
    API_BASE_ENV,
    DEFAULT_API_BASE,
    ApiBaseSource,
    api_base_from_server,
    configured_api_base,
    normalise_api_base,
    probe_api_base,
    resolve_api_base,
    resolve_api_base_detail,
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


def _serve(handler: Callable[..., http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    """A throwaway HTTP server on an ephemeral port, yielded as an api_base."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _responder(status: int, body: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's spelling
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            """Silence. The suite's output is not a web server access log."""

    return Handler


@pytest.fixture
def fake_agentjobs() -> Iterator[str]:
    """Answers ``/api/version`` the way this application does."""
    body = json.dumps({"version": "0.1.0", "schema_version": 2}).encode()
    yield from _serve(_responder(200, body))


@pytest.fixture
def stranger_server() -> Iterator[str]:
    """Answers 200, but not as AgentJobs -- a port someone else got to first."""
    yield from _serve(_responder(200, b"<html>some other service</html>"))


@pytest.fixture
def not_found_server() -> Iterator[str]:
    """Answers 404 -- an HTTP server that has never heard of ``/api/version``."""
    yield from _serve(_responder(404, b"not found"))


@pytest.fixture
def closed_port() -> str:
    """An address on this machine with nothing behind it.

    Bound and released so the port is known to be free, which is as close to a
    guarantee as this gets without holding it open -- and holding it open is exactly
    what would make it answer.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


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


class TestWhichSourceAnswered:
    """The address alone cannot tell a reader which line to go and edit."""

    def test_an_observed_address_is_marked_observed(self) -> None:
        resolved = resolve_api_base_detail("http://127.0.0.1:8876/")
        assert resolved.value == "http://127.0.0.1:8876"
        assert resolved.source is ApiBaseSource.OBSERVED

    def test_the_environment_is_named(self, monkeypatch) -> None:
        monkeypatch.setenv(API_BASE_ENV, "http://127.0.0.1:9001")
        resolved = resolve_api_base_detail()
        assert resolved.source is ApiBaseSource.ENVIRONMENT
        assert API_BASE_ENV in resolved.describe_source()

    def test_the_config_file_is_named_by_path(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://127.0.0.1:8876")
        resolved = resolve_api_base_detail(home=isolated_home)
        assert resolved.source is ApiBaseSource.CONFIG
        assert str(isolated_home / CONFIG_FILENAME) in resolved.describe_source(isolated_home)

    def test_a_machine_that_declared_nothing_says_so(self, isolated_home: Path) -> None:
        """The distinction task-193 exists for: a fallback is not an answer."""
        resolved = resolve_api_base_detail(home=isolated_home)
        assert resolved.value == DEFAULT_API_BASE
        assert resolved.source is ApiBaseSource.FALLBACK
        assert "nothing on this machine declared" in resolved.describe_source(isolated_home)

    def test_the_value_half_still_agrees_with_the_detailed_half(self, isolated_home: Path) -> None:
        write_config(isolated_home, api_base="http://127.0.0.1:8876")
        assert (
            resolve_api_base(home=isolated_home)
            == resolve_api_base_detail(home=isolated_home).value
        )


class TestDoesAnythingAnswerThere:
    """A resolved address is a claim about a port. This is the only thing that checks it.

    Driven against a real socket rather than a mocked ``urlopen``: what is being pinned
    is which of three answers each real-world shape produces, and a stub that returns
    the shape under test proves only that the assertion was written down twice.
    """

    def test_an_agentjobs_server_is_recognised(self, fake_agentjobs: str) -> None:
        probe = probe_api_base(fake_agentjobs)
        assert probe.answered and probe.is_agentjobs
        assert "AgentJobs answered" in probe.detail

    def test_a_stranger_on_the_port_answers_but_is_not_agentjobs(
        self, stranger_server: str
    ) -> None:
        """Something is listening. Worth saying, not worth refusing over -- an AgentJobs
        old enough to have no ``/api/version`` presents identically."""
        probe = probe_api_base(stranger_server)
        assert probe.answered
        assert not probe.is_agentjobs

    def test_a_404_still_counts_as_answering(self, not_found_server: str) -> None:
        probe = probe_api_base(not_found_server)
        assert probe.answered
        assert not probe.is_agentjobs
        assert "404" in probe.detail

    def test_a_closed_port_answers_nothing(self, closed_port: str) -> None:
        probe = probe_api_base(closed_port)
        assert not probe.answered
        assert not probe.is_agentjobs
        assert "nothing answered" in probe.detail

    def test_a_trailing_slash_does_not_produce_a_double_slash(self, fake_agentjobs: str) -> None:
        assert probe_api_base(fake_agentjobs + "/").is_agentjobs

    def test_a_proxy_in_the_environment_is_ignored(self, fake_agentjobs: str, monkeypatch) -> None:
        """Loopback asked through a proxy is the proxy's loopback, not ours."""
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        assert probe_api_base(fake_agentjobs).is_agentjobs
