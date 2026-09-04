"""Where the front door's secret comes from, and what happens when there isn't one.

The rule that uses this value lives in ``agentjobs.principals`` and is tested there.
What is tested here is only the supply: environment over file, a missing file meaning
"no front door" rather than an error, and the cache being short enough that starting the
server before the proxy is not a footgun.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs import front_door
from agentjobs.front_door import SECRET_ENV, current_secret, secret_path
from agentjobs.projects import HOME_ENV

SECRET = "0123456789abcdef"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    front_door.reset_cache()
    return tmp_path


def test_no_file_and_no_environment_means_no_front_door(home: Path) -> None:
    """The ordinary state of a machine running no proxy, and it is not an error.

    ``None`` is what makes the identity header inert on such a machine: nothing can be
    the front door, so nothing is believed to be.
    """
    assert current_secret() is None


def test_the_file_is_read_and_stripped(home: Path) -> None:
    secret_path().write_text(f"  {SECRET}\n", encoding="utf-8")
    front_door.reset_cache()
    assert current_secret() == SECRET


def test_an_empty_file_is_not_a_secret(home: Path) -> None:
    """An empty secret must never match an empty presented header.

    ``is_front_door`` refuses a falsy secret outright, and this keeps the two ends
    agreeing: a truncated or half-written file is no front door, not a front door
    anybody can walk through.
    """
    secret_path().write_text("\n", encoding="utf-8")
    front_door.reset_cache()
    assert current_secret() is None


def test_the_environment_wins_over_the_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_path().write_text("from-the-file", encoding="utf-8")
    front_door.reset_cache()
    monkeypatch.setenv(SECRET_ENV, "  from-the-environment  ")
    assert current_secret() == "from-the-environment"


def test_a_file_written_after_startup_is_picked_up(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason this is not read once at import.

    A server started before the proxy would otherwise treat every remote caller as the
    machine owner until somebody restarted it, and nothing about that failure would say
    so. The cache is time-boxed, so start order does not matter; the clock is stepped
    here rather than slept through.
    """
    assert current_secret() is None
    secret_path().write_text(SECRET, encoding="utf-8")
    assert current_secret() is None, "still cached, which is the point of the cache"

    class _Later:
        """A clock the module's own ``time`` name resolves to, moved past the cache.

        The attribute is replaced on the module rather than on ``time`` itself, so
        nothing else running in this worker gets a doctored clock.
        """

        @staticmethod
        def monotonic() -> float:
            return real() + front_door._FILE_CACHE_SECONDS + 1

    real = front_door.time.monotonic
    monkeypatch.setattr(front_door, "time", _Later)
    assert current_secret() == SECRET


def test_an_unreadable_secret_is_the_same_as_an_absent_one(home: Path) -> None:
    """A directory where a file should be: misconfigured, and it fails closed.

    Raising here would take the whole application down over an optional proxy's
    configuration; resolving every caller as the owner they already are does not.
    """
    secret_path().mkdir()
    front_door.reset_cache()
    assert current_secret() is None


def test_the_path_is_the_one_the_proxy_computes(home: Path) -> None:
    """Both ends compute ``<AGENTJOBS_HOME>/front-door-secret`` and neither is told it.

    The Go side asserts its own half in ``frontdoor_test.go``; if these two ever
    disagree the symptom is silent -- every remote caller becomes the owner -- so each
    side pins the spelling rather than trusting the other's.
    """
    assert secret_path() == home / "front-door-secret"
