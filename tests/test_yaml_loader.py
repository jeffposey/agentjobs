"""The libyaml read path must agree with the pure-Python one, everywhere.

Speed bought by parsing things differently is not speed, it is a data-corruption bug
with a good benchmark. So the parity is asserted against the real corpus rather than
against a handful of hand-written samples, and the awkward cases -- unicode, folded
multi-line prose, timestamps, the empty document -- are pinned individually because
those are where the two parsers could plausibly diverge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from yaml import SafeLoader

from agentjobs.api.main import app
from agentjobs.storage import YAML_LOADER, TaskStorage, load_yaml, yaml_loader_name

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "tasks" / "agentjobs"

csafe = pytest.importorskip("yaml", reason="PyYAML is required")
HAS_LIBYAML = hasattr(yaml, "CSafeLoader")


def _corpus_files() -> list[Path]:
    return sorted(CORPUS.glob("*.yaml"))


@pytest.mark.skipif(not HAS_LIBYAML, reason="libyaml is not installed here")
@pytest.mark.parametrize("path", _corpus_files(), ids=lambda path: path.name)
def test_every_real_task_file_loads_identically_under_both_loaders(path: Path) -> None:
    """The check that matters: the actual data, not a sample of it."""
    text = path.read_text(encoding="utf-8")
    assert load_yaml(text) == yaml.load(text, Loader=SafeLoader)


@pytest.mark.parametrize(
    ("label", "document"),
    [
        ("empty", ""),
        ("only comments", "# nothing here\n"),
        ("unicode", 'title: "héllo wörld — em dash, ellipsis…"\n'),
        ("emoji", 'title: "🚀 ship it"\n'),
        (
            "folded multi-line prose",
            "body: >\n  first line\n  second line\n\n  new paragraph\n",
        ),
        (
            "literal block with trailing newline",
            "body: |\n  line one\n  line two\n",
        ),
        ("timestamp", "ts: 2026-08-17T23:51:43.310915Z\n"),
        ("date only", "when: 2026-08-17\n"),
        ("null forms", "a: null\nb: ~\nc:\n"),
        ("bools and numbers", "a: true\nb: 0755\nc: 1_000\nd: 1e3\ne: .inf\n"),
        ("quoted string that looks numeric", 'version: "1.10"\n'),
        ("escaped double quotes", 'body: "she said \\"no\\" twice"\n'),
        ("nested structures", "a:\n  - b: 1\n    c: [1, 2, 3]\n  - d: {e: f}\n"),
        ("tabs inside a quoted scalar", 'body: "a\\tb"\n'),
        ("windows line endings", "a: 1\r\nb: 2\r\n"),
    ],
)
@pytest.mark.skipif(not HAS_LIBYAML, reason="libyaml is not installed here")
def test_awkward_documents_parse_identically(label: str, document: str) -> None:
    assert load_yaml(document) == yaml.load(document, Loader=SafeLoader), label


def test_load_yaml_is_a_drop_in_for_safe_load() -> None:
    """Same answer as the call it replaced, so no caller had to change."""
    document = "a: 1\nb: [2, 3]\nc: {d: e}\n"
    assert load_yaml(document) == yaml.safe_load(document)


def test_the_loader_refuses_arbitrary_python_objects() -> None:
    """Safety is the reason this is CSafeLoader and not CLoader.

    A faster parser that also executes what it reads would be a straight trade of a
    security property for a performance one.
    """
    with pytest.raises(yaml.YAMLError):
        load_yaml("!!python/object/apply:os.system ['echo pwned']\n")


def test_the_loader_in_use_is_named() -> None:
    assert yaml_loader_name() == YAML_LOADER
    assert YAML_LOADER


@pytest.mark.skipif(not HAS_LIBYAML, reason="libyaml is not installed here")
def test_libyaml_is_actually_being_used_here() -> None:
    """Guards against a silent fallback on the machine that runs the benchmarks.

    Without this, an environment that lost the C extension would simply get slower and
    the report would say so in a line nobody reads.
    """
    assert "libyaml" in YAML_LOADER


def test_the_fallback_is_announced_not_silent(monkeypatch, caplog) -> None:
    """An install without libyaml must say so, or a 13x regression looks like a mystery."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def without_libyaml(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml" and fromlist and "CSafeLoader" in fromlist:
            raise ImportError("simulated: no libyaml")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", without_libyaml)
    import agentjobs.storage as storage_module

    with caplog.at_level("WARNING"):
        reloaded = importlib.reload(storage_module)
        assert "pure-python" in reloaded.YAML_LOADER
        assert "libyaml" in caplog.text.lower()
        # And it still works, which is the point of a fallback.
        assert reloaded.load_yaml("a: 1\n") == {"a": 1}

    monkeypatch.undo()
    importlib.reload(storage_module)


def test_storage_reads_a_task_through_the_fast_loader(tmp_path: Path) -> None:
    """End to end: the loader swap did not change what storage returns."""
    from datetime import datetime, timezone

    from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    storage = TaskStorage(tmp_path)
    storage.save_task(
        Task(
            id="task-001",
            title="Unicode: héllo — wörld 🚀",
            created=now,
            updated=now,
            lifecycle=Lifecycle.READY,
            ball=Ball.AGENT,
            ball_reason=BallReason.AVAILABLE,
            priority=Priority.MEDIUM,
            category="testing",
            spec=Spec(summary="Summary", description="Line one\nLine two\n\nParagraph"),
        )
    )
    loaded = storage.load_task("task-001")
    assert loaded is not None
    assert loaded.title == "Unicode: héllo — wörld 🚀"
    assert loaded.spec.description == "Line one\nLine two\n\nParagraph"


def test_the_version_endpoint_reports_the_loader() -> None:
    """Discoverable at runtime, not only in a benchmark someone has to run."""
    response = TestClient(app).get("/api/version")
    assert response.status_code == 200
    assert response.json()["yaml_loader"] == yaml_loader_name()
