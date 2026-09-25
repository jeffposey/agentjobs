"""Finding and stopping every listener on a port, on both address families (task-601).

A loopback server listens on 127.0.0.1 and ::1 (task-483). On 2026-09-25 a server was
left holding ::1 alone: the tailnet proxy, which dials 127.0.0.1, was refused, and
`restart` read only the first netstat line, so it never saw the stale holder.
"""

from __future__ import annotations

import socket
import subprocess
from typing import List

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from agentjobs import cli
from agentjobs.cli import app

runner = CliRunner()

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1180
  TCP    127.0.0.1:8876         0.0.0.0:0              LISTENING       442468
  TCP    127.0.0.1:8876         127.0.0.1:55881        ESTABLISHED     442468
  TCP    127.0.0.1:18876        0.0.0.0:0              LISTENING       999
  TCP    [::1]:8876             [::]:0                 LISTENING       442468
  TCP    [::1]:8876             [::]:0                 LISTENING       436948
  TCP    [::1]:88760            [::]:0                 LISTENING       777
  UDP    127.0.0.1:8876         *:*                                    555
"""


def _netstat(stdout: str) -> MagicMock:
    return MagicMock(return_value=subprocess.CompletedProcess(["netstat"], 0, stdout, ""))


def test_windows_listeners_cover_both_families_and_match_the_port_exactly() -> None:
    with patch("platform.system", return_value="Windows"), patch(
        "subprocess.run", _netstat(NETSTAT)
    ):
        listeners = cli._port_listeners(8876)
        pids = cli._find_processes_by_port(8876)

    assert listeners == [("127.0.0.1", 442468), ("::1", 442468), ("::1", 436948)]
    # The stale IPv6-only holder is found, not hidden behind the first line.
    assert pids == [442468, 436948]


def test_lsof_listeners_are_parsed_per_process() -> None:
    lsof = "p10\nf3\nn127.0.0.1:8876\nf4\nn[::1]:8876\np20\nf5\nn[::1]:8876\n"
    with (
        patch("platform.system", return_value="Linux"),
        patch(
            "subprocess.run",
            MagicMock(return_value=subprocess.CompletedProcess(["lsof"], 0, lsof, "")),
        ),
    ):
        assert cli._port_listeners(8876) == [("127.0.0.1", 10), ("::1", 10), ("::1", 20)]


@pytest.mark.parametrize(
    ("listeners", "warns"),
    [
        ([], False),
        ([("127.0.0.1", 1), ("::1", 1)], False),
        ([("127.0.0.1", 1)], False),
        ([("::1", 1)], True),
    ],
)
def test_ipv6_only_is_the_state_that_warns(listeners: list, warns: bool) -> None:
    with patch("agentjobs.cli._port_listeners", return_value=listeners):
        assert bool(cli._missing_ipv4_warning(8876)) is warns


def test_restart_stops_every_listener_before_serving() -> None:
    with (
        patch("agentjobs.cli._find_processes_by_port", side_effect=[[442468, 436948], []]),
        patch("agentjobs.cli._stop_server", return_value=True) as stop_one,
        patch("agentjobs.cli._run_server") as serve,
        patch("agentjobs.cli._warn_if_bundle_missing"),
    ):
        result = runner.invoke(app, ["restart", "--host", "127.0.0.1", "--port", "8876"])

    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in stop_one.call_args_list] == [442468, 436948]
    assert "PIDs 442468, 436948" in result.output
    serve.assert_called_once_with("127.0.0.1", 8876, False)


def test_stop_stops_every_listener() -> None:
    with (
        patch("agentjobs.cli._find_processes_by_port", side_effect=[[1, 2], []]),
        patch("agentjobs.cli._stop_server", return_value=True) as stop_one,
    ):
        result = runner.invoke(app, ["stop", "--port", "8876"])

    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in stop_one.call_args_list] == [1, 2]
    assert "stopped successfully" in result.output


def test_stop_fails_when_a_listener_survives() -> None:
    with (
        patch("agentjobs.cli._find_processes_by_port", side_effect=[[1, 2], [2]]),
        patch("agentjobs.cli._stop_server", return_value=True),
    ):
        result = runner.invoke(app, ["stop", "--port", "8876"])

    assert result.exit_code == 1


def test_status_refuses_two_servers_on_one_port() -> None:
    with patch("agentjobs.cli._port_listeners", return_value=[("127.0.0.1", 1), ("::1", 2)]):
        result = runner.invoke(app, ["status", "--port", "8876"])

    assert result.exit_code == 1
    assert "2 processes are listening" in result.output


def test_status_is_healthy_on_both_families() -> None:
    with patch("agentjobs.cli._port_listeners", return_value=[("127.0.0.1", 7), ("::1", 7)]):
        result = runner.invoke(app, ["status", "--port", "8876"])

    assert result.exit_code == 0, result.output
    assert "PID 7" in result.output


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


needs_ipv6 = pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 loopback")


@needs_ipv6
def test_status_sees_a_real_ipv6_only_listener() -> None:
    """The incident's state, built for real and read through the real netstat/lsof."""
    port = _free_port()
    try:
        holder = socket.create_server(("::1", port), family=socket.AF_INET6)
    except OSError:
        pytest.skip("::1 cannot be bound here")
    with holder:
        result = runner.invoke(app, ["status", "--port", str(port)])

    assert result.exit_code == 1
    assert "IPv6 only" in result.output


@needs_ipv6
def test_serve_refuses_to_start_beside_an_ipv6_holder() -> None:
    port = _free_port()
    try:
        holder = socket.create_server(("::1", port), family=socket.AF_INET6)
    except OSError:
        pytest.skip("::1 cannot be bound here")
    with holder:
        with pytest.raises(OSError, match="already held by another process"):
            cli._loopback_sockets(port)
        # The IPv4 socket it had already bound is released, not leaked.
        socket.create_server(("127.0.0.1", port), family=socket.AF_INET).close()


def test_windows_serves_on_the_selector_loop() -> None:
    """The proactor loop closes a listener when one accept fails (task-601)."""
    import asyncio

    seen: List[str] = []

    class FakeServer:
        config = MagicMock()

        def run(self, sockets: list) -> None:
            seen.append("proactor-or-default")

        async def serve(self, sockets: list) -> None:
            seen.append(type(asyncio.get_running_loop()).__name__)

    with patch("sys.platform", "win32"):
        cli._serve_sockets(FakeServer(), ["v4"])

    assert len(seen) == 1
    assert "Selector" in seen[0]
