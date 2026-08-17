"""The Claude Code half of the plugin directory.

One directory serves both clients: they read different manifest paths and ignore each
other's, so `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json` sit side by
side over one `.mcp.json`, one skill, and one guard. The alternative was two directories
with the guard duplicated, and a marketplace installs a directory -- a shared module
outside it is simply not there after install.

The skill and MCP wiring are shared, so their content is asserted once in
`test_codex_plugin.py` rather than twice here. What is here is what is genuinely
Claude's: its manifest, and the frontmatter requirement that only matters to it.

What these cannot check is that Claude Code loads it. That needs Claude Code, and is
recorded on task-122 as human verification rather than asserted from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentjobs.__version__ import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agentjobs"
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
CODEX_MANIFEST = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
SKILL = (PLUGIN / "skills" / "agentjobs" / "SKILL.md").read_text(encoding="utf-8")
README = (PLUGIN / "README.md").read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


class TestManifest:
    def test_it_declares_the_expected_shape(self):
        assert MANIFEST["name"] == "agentjobs"
        assert MANIFEST["mcpServers"] == "./.mcp.json"
        assert MANIFEST["skills"] == ["skills/agentjobs"]
        assert MANIFEST["hooks"] == "./hooks/hooks-claude.json"

    def test_the_plugin_version_tracks_the_package_version(self):
        """They ship from one release; a drift means somebody upgraded half of it."""
        assert MANIFEST["version"] == __version__

    def test_every_referenced_path_exists(self):
        assert (PLUGIN / MANIFEST["mcpServers"]).exists()
        for skill in MANIFEST["skills"]:
            assert (PLUGIN / skill / "SKILL.md").exists()
        assert (PLUGIN / MANIFEST["hooks"]).exists()

    def test_it_points_at_the_same_server_and_skill_as_the_codex_manifest(self):
        """One directory, two manifests. If these diverge, the clients are being given
        different plugins under one name and the drift is invisible until one breaks."""
        assert MANIFEST["name"] == CODEX_MANIFEST["name"]
        assert MANIFEST["version"] == CODEX_MANIFEST["version"]
        assert MANIFEST["description"] == CODEX_MANIFEST["description"]
        assert MANIFEST["skills"] == CODEX_MANIFEST["skills"]
        assert (PLUGIN / MANIFEST["mcpServers"]).resolve() == (
            PLUGIN / CODEX_MANIFEST["mcpServers"]
        ).resolve()

    def test_the_two_manifests_register_different_hooks(self):
        """The only thing they may legitimately disagree about: each client's entry
        point and its own tool vocabulary."""
        assert MANIFEST["hooks"] != CODEX_MANIFEST["hooks"]


class TestMarketplace:
    """The repository root is the marketplace a user adds.

    Without this file `claude plugin install` has nothing to resolve, and the install
    instructions are an untestable sentence — which is what they were until somebody
    tried to follow them. The Codex marketplace entry lives at a different path
    (.agents/plugins/marketplace.json) and is not covered here.
    """

    def test_it_declares_a_marketplace_named_for_the_project(self):
        assert MARKETPLACE["name"] == "agentjobs"
        assert MARKETPLACE["owner"]["name"]
        assert MARKETPLACE["description"]

    def test_it_lists_the_plugin_and_the_source_resolves_to_a_real_plugin(self):
        """A source path that does not contain a plugin manifest fails at install
        time, on the user's machine, with the repository looking fine from here."""
        entry = next(item for item in MARKETPLACE["plugins"] if item["name"] == "agentjobs")

        source = ROOT / entry["source"]
        assert source.is_dir(), entry["source"]
        assert (source / ".claude-plugin" / "plugin.json").exists()

    def test_the_marketplace_and_plugin_agree_on_the_plugin_name(self):
        """`claude plugin install agentjobs@agentjobs` resolves the marketplace entry;
        the installed plugin then identifies itself by its own manifest. A mismatch
        installs cleanly and then shows up under a name the docs never mention."""
        entry = next(item for item in MARKETPLACE["plugins"] if item["name"] == "agentjobs")

        assert entry["name"] == MANIFEST["name"]

    def test_the_source_is_relative_so_it_works_from_a_clone_or_the_remote(self):
        entry = next(item for item in MARKETPLACE["plugins"] if item["name"] == "agentjobs")

        assert entry["source"].startswith("./")
        for marker in ("C:/Users", "C:\\\\Users", "/home/", "/Users/"):
            assert marker not in json.dumps(MARKETPLACE), marker


class TestNoSecretsOrMachinePaths:
    @pytest.mark.parametrize("content", [json.dumps(MANIFEST)])
    def test_no_machine_specific_absolute_path_is_embedded(self, content):
        for marker in ("C:\\\\Users", "C:/Users", "/home/", "/Users/", "AppData"):
            assert marker not in content, marker

    @pytest.mark.parametrize("content", [json.dumps(MANIFEST)])
    def test_no_credential_shaped_string_is_embedded(self, content):
        lowered = content.lower()
        for marker in ("api_key", "apikey", "secret", "password", "token=", "bearer "):
            assert marker not in lowered, marker


class TestSkillFrontmatter:
    def test_the_skill_names_itself_rather_than_relying_on_its_directory(self):
        """Claude falls back to the directory basename when `name` is absent. A
        marketplace install puts the skill under a versioned directory, so the fallback
        is a name that changes on every upgrade."""
        assert SKILL.startswith("---\n")
        assert "name: agentjobs" in SKILL.split("---")[1]


class TestReadme:
    def test_it_documents_the_claude_install(self):
        assert "## Install" in README
        assert "claude" in flat(README).lower()

    def test_it_does_not_still_say_claude_gets_no_guard(self):
        """The sentence this task exists to make false."""
        lowered = flat(README).lower()
        for stale in (
            "no pre-tool hook is shipped for claude",
            "claude, gemini — install the standalone",
        ):
            assert stale not in lowered, stale

    def test_it_is_honest_that_the_guard_is_not_a_security_boundary(self):
        assert "guardrail, not a security boundary" in flat(README)
        assert "does not make direct writes impossible" in flat(README)
