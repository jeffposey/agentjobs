"""The Codex plugin's manifest, wiring, and skill.

These are packaging assertions, and the failures they catch are the boring ones that
waste an afternoon: a version that drifted from the package, an absolute path from
somebody's laptop, a skill that quietly tells an agent to edit YAML.

What they cannot check is that Codex loads it. That needs Codex, and is recorded on
task-116 as human verification rather than asserted from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentjobs.__version__ import __version__

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "agentjobs"
MANIFEST = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
MCP_CONFIG = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
SKILL = (PLUGIN / "skills" / "agentjobs" / "SKILL.md").read_text(encoding="utf-8")
README = (PLUGIN / "README.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Collapse whitespace so an assertion is about wording, not line wrapping.

    Without this, reflowing a paragraph breaks a test that has no opinion about
    where the lines end -- which teaches everyone to stop trusting the test.
    """
    return " ".join(text.split())


FLAT_SKILL = flat(SKILL)
FLAT_README = flat(README)

ALL_TOOLS = [
    "projects_list",
    "tasks_list",
    "task_get",
    "tasks_search",
    "task_next",
    "task_create_draft",
    "task_create_ready",
    "task_promote",
    "task_claim",
    "task_release",
    "task_handoff",
    "task_close",
    "task_log_append",
    "task_update_content",
]


class TestManifest:
    def test_it_declares_the_expected_shape(self):
        assert MANIFEST["name"] == "agentjobs"
        assert MANIFEST["mcpServers"] == ".mcp.json"
        assert MANIFEST["skills"] == ["skills/agentjobs"]
        assert MANIFEST["hooks"] == "hooks/hooks.json"

    def test_the_plugin_version_tracks_the_package_version(self):
        """They ship from one release; a drift means somebody upgraded half of it."""
        assert MANIFEST["version"] == __version__

    def test_every_referenced_path_exists(self):
        assert (PLUGIN / MANIFEST["mcpServers"]).exists()
        for skill in MANIFEST["skills"]:
            assert (PLUGIN / skill / "SKILL.md").exists()
        assert (PLUGIN / MANIFEST["hooks"]).exists()


class TestMcpWiring:
    def test_it_launches_the_installed_command_and_vendors_nothing(self):
        """One server, two distributions. A vendored copy is a second thing to fix."""
        server = MCP_CONFIG["mcpServers"]["agentjobs"]

        assert server["command"] == "agentjobs"
        assert server["args"] == ["mcp"]
        assert "python" not in json.dumps(server).lower()

    def test_the_configured_url_is_the_documented_default(self):
        assert MCP_CONFIG["mcpServers"]["agentjobs"]["env"]["AGENTJOBS_URL"] == (
            "http://127.0.0.1:8765"
        )


class TestNoSecretsOrMachinePaths:
    @pytest.mark.parametrize(
        "content", [json.dumps(MANIFEST), json.dumps(MCP_CONFIG), SKILL, README]
    )
    def test_no_machine_specific_absolute_path_is_embedded(self, content):
        """A path from one laptop is wrong for every other machine."""
        for marker in ("C:\\\\Users", "C:/Users", "/home/", "/Users/", "AppData"):
            assert marker not in content, marker

    @pytest.mark.parametrize(
        "content", [json.dumps(MANIFEST), json.dumps(MCP_CONFIG), SKILL, README]
    )
    def test_no_credential_shaped_string_is_embedded(self, content):
        lowered = content.lower()
        for marker in ("api_key", "apikey", "secret", "password", "token=", "bearer "):
            assert marker not in lowered, marker


class TestSkillContent:
    def test_it_declares_the_frontmatter_a_skill_needs(self):
        assert SKILL.startswith("---\n")
        assert "name: agentjobs" in SKILL
        assert "description:" in SKILL

    def test_the_description_covers_the_situations_it_should_trigger_on(self):
        header = SKILL.split("---")[1].lower()

        for trigger in ("task", "project", "claim", "hand off", "backlog", "progress"):
            assert trigger in header, trigger

    def test_it_teaches_discovery_before_anything_else(self):
        assert SKILL.index("projects_list") < SKILL.index("task_claim")
        assert "only tool that does not take a `project_id`" in FLAT_SKILL

    def test_it_requires_an_explicit_project_even_with_one_project(self):
        assert "even when there is only one project" in FLAT_SKILL

    def test_it_forbids_adopting_default_user_as_an_actor(self):
        assert "Never send a model name" in FLAT_SKILL
        assert "`default_user`" in SKILL

    def test_it_teaches_the_zero_context_reading_order(self):
        for element in ("ball_prompt", "newest-first", "Decisions are binding", "unmet_needs"):
            assert element in SKILL, element

    def test_it_teaches_operation_ids_and_revisions(self):
        assert "resend it with the same id" in FLAT_SKILL
        assert "expected_revision" in SKILL
        assert "replayed" in SKILL

    def test_it_names_every_tool_the_server_publishes(self):
        for tool in ALL_TOOLS:
            assert tool in SKILL, tool

    def test_it_says_yaml_is_readable_generated_state(self):
        assert "generated state" in SKILL
        assert "Read it freely; never edit it." in FLAT_SKILL

    def test_it_never_offers_direct_yaml_as_an_ordinary_fallback(self):
        """The one sentence that would undo the entire program if it were missing."""
        assert "A failing tool is not permission to edit YAML." in FLAT_SKILL
        assert "emergency-recovery procedure only" in FLAT_SKILL

    def test_it_names_rest_and_cli_as_the_availability_fallback(self):
        assert "REST API and the `agentjobs` CLI" in FLAT_SKILL

    def test_it_does_not_teach_a_generic_state_setter(self):
        for forbidden in ("set_status", "set_lifecycle", "save_yaml", "update_task("):
            assert forbidden not in SKILL, forbidden

    def test_it_carries_the_two_repository_rules_the_tools_cannot_enforce(self):
        assert "worktree" in SKILL
        assert "committed to `main`" in FLAT_SKILL

    def test_it_does_not_restate_tool_schemas(self):
        """MCP already publishes them; a copy here is a copy that goes stale."""
        assert "Read them from the tool list" in FLAT_SKILL
        assert '"type": "object"' not in SKILL


class TestReadme:
    def test_it_documents_install_verify_upgrade_and_rollback(self):
        for section in ("## Install", "## Verify", "## Upgrade and rollback"):
            assert section in README, section

    def test_it_says_a_new_session_is_required(self):
        assert "start a new Codex session" in README.lower() or "new Codex session" in README

    def test_it_states_that_one_config_serves_cli_desktop_and_ide(self):
        assert "desktop app, the CLI, and the IDE extension" in FLAT_README

    def test_it_is_honest_that_the_guard_is_not_a_security_boundary(self):
        """Claiming enforcement this plugin does not provide would be the worst
        possible error in this file."""
        assert "guardrail, not a security boundary" in FLAT_README
        assert "does not make direct writes impossible" in FLAT_README
        for bypass in ("hosted", "disable", "obfuscated"):
            assert bypass in FLAT_README.lower(), bypass

    def test_it_documents_trusting_and_disabling_the_hook(self):
        assert "review and trust" in FLAT_README
        assert "### Trusting and disabling it" in README
