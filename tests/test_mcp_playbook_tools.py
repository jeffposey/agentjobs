"""``playbooks_list``: what agents may learn about playbooks, and what they may not do.

Driven against the real FastAPI application over the real route, like the read-tool
suite: the tool is thin, so what is worth testing is whether the shape survives the
round trip -- and whether the *absence* of a run tool is still true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Tuple

import anyio
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from mcp import types

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.mcp.errors import ErrorCode, ToolError
from agentjobs.mcp.inventory import build_registry
from agentjobs.mcp.tools import ToolRegistry
from agentjobs.playbooks import install_references
from agentjobs.projects import ProjectRegistry

VALID = """---
name: groom
description: Find duplicates and propose closures.
target: project
difficulty: hard
verbs: [close, log]
gates:
  - before: close
    what: A human approved the list.
---

# Groom

The brief.
"""


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> Iterator[Tuple[ToolRegistry, Path]]:
    """One registered project, with no playbooks directory yet."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "alpha"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Alpha", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    (root / "tasks").mkdir()
    ProjectRegistry(home=tmp_path / "home").add(root, project_id="alpha")

    with TestClient(app) as http:
        client = TaskClient("http://testserver", client=http)
        yield build_registry(client), root

    reset_dependency_cache()


def call(registry: ToolRegistry, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Invoke one tool and return its payload, validated against its own schema."""

    async def run() -> Dict[str, Any]:
        definition = registry.get(name)
        result = await definition.handler(arguments)
        assert not isinstance(result, types.CallToolResult), "tool reported an error"
        content, structured = result
        # A text-only client must get something readable too, not just the payload.
        assert content and isinstance(content[0], types.TextContent)
        assert content[0].text.strip()
        jsonschema.validate(instance=structured, schema=definition.output_schema)
        assert isinstance(structured, dict)
        return structured

    return anyio.run(run)


class TestContract:
    def test_the_tool_is_published_and_annotated_read_only(self, service) -> None:
        registry, _ = service
        definition = registry.get("playbooks_list")
        assert definition.annotations.readOnlyHint is True
        assert definition.annotations.destructiveHint is False
        assert definition.description.strip()

    def test_no_playbook_mutation_tool_exists(self, service) -> None:
        """Design P10: an agent starting a run is an agent causing a dispatch."""
        registry, _ = service
        playbook_tools = [name for name in registry.names if "playbook" in name]
        assert playbook_tools == ["playbooks_list"]

    def test_nothing_in_the_mcp_package_can_reach_the_run_path(self) -> None:
        """The name check above is necessary and not sufficient.

        ``playbook_tools == ["playbooks_list"]`` only sees a tool with "playbook" in its
        name; a tool called anything else could still call ``run_playbook``. This reads
        the package instead. Decision P10 is about what an agent can *cause*, not about
        naming, and the guarantee has to be checked the same way.
        """
        import agentjobs.mcp as mcp_package

        root = Path(mcp_package.__file__).parent
        offenders = [
            path.name
            for path in root.rglob("*.py")
            if "playbooks.run" in path.read_text(encoding="utf-8")
            or "run_playbook" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_it_requires_a_project_id(self, service) -> None:
        registry, _ = service
        with pytest.raises(ToolError) as excinfo:
            call(registry, "playbooks_list", {})
        assert excinfo.value.code is ErrorCode.INVALID_INPUT

    def test_an_unknown_project_names_the_ones_that_exist(self, service) -> None:
        registry, _ = service
        with pytest.raises(ToolError) as excinfo:
            call(registry, "playbooks_list", {"project_id": "nope"})
        assert excinfo.value.code is ErrorCode.UNKNOWN_PROJECT


class TestListing:
    def test_a_project_with_no_directory_says_so_rather_than_failing(self, service) -> None:
        registry, _ = service
        payload = call(registry, "playbooks_list", {"project_id": "alpha"})
        assert payload["exists"] is False
        assert payload["playbooks"] == []

    def test_playbooks_are_returned_with_their_contracts(self, service) -> None:
        registry, root = service
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        payload = call(registry, "playbooks_list", {"project_id": "alpha"})
        assert payload["exists"] is True
        entry = payload["playbooks"][0]
        assert entry["name"] == "groom"
        assert entry["target"] == "project"
        assert entry["gates"][0]["before"] == "close"

    def test_the_brief_is_not_shipped_in_the_listing(self, service) -> None:
        """Discovery, not a way to pull every brief into an agent's context."""
        registry, root = service
        install_references(root / "playbooks")
        payload = call(registry, "playbooks_list", {"project_id": "alpha"})
        assert [item["name"] for item in payload["playbooks"]] == [
            "flesh-out",
            "groom",
            "reorder",
        ]
        assert all("body" not in item for item in payload["playbooks"])

    def test_a_file_that_does_not_validate_is_reported_not_hidden(self, service) -> None:
        registry, root = service
        (root / "playbooks").mkdir()
        (root / "playbooks" / "groom.md").write_text(VALID, encoding="utf-8")
        (root / "playbooks" / "broken.md").write_text(
            VALID.replace("difficulty: hard", "difficulty: extreme"), encoding="utf-8"
        )
        payload = call(registry, "playbooks_list", {"project_id": "alpha"})
        assert [item["name"] for item in payload["playbooks"]] == ["groom"]
        assert payload["problems"][0]["filename"] == "broken.md"
