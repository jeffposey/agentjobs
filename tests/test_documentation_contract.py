"""Regression coverage for the agent resumption guide used by dispatch prompts."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_GUIDE = ROOT / "docs" / "agent-workflow.md"
DISPATCH_DESIGN = ROOT / "docs" / "agent-dispatch-design.md"


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("README.md", "packaged React web application"),
        ("ENGINEERING.md", "packaged React Web UI"),
        ("docs/index.md", "packaged React web application"),
        ("docs/installation.md", "platform-independent `py3-none-any` wheel"),
        ("docs/quickstart.md", "primary UI opens at `http://localhost:8765/app/`"),
        ("frontend/README.md", "React owns the current design"),
    ],
)
def test_current_documentation_is_react_first(path: str, required: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert required in text


def test_readme_describes_the_responsive_react_product() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## The React application" in text
    assert "desktop and laptop browsers, tablets, and phones" in text
    assert "touch-friendly sizing" in text
    assert "Progressive Web App (PWA)" in text


@pytest.mark.parametrize("path", ["README.md", "ENGINEERING.md", "docs/index.md"])
def test_primary_entry_points_do_not_present_server_rendering_as_the_ui(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8").lower()
    assert "a server-rendered web ui" not in text
    if "jinja" in text:
        assert "legacy" in text or "historical" in text or "compatibility" in text


def test_dispatch_guide_exists_and_links_to_canonical_resumption_contract() -> None:
    """A dispatched agent must not be pointed at a missing or self-invented contract."""
    assert WORKFLOW_GUIDE.is_file()

    guide = WORKFLOW_GUIDE.read_text(encoding="utf-8")
    dispatch_design = DISPATCH_DESIGN.read_text(encoding="utf-8")

    assert "docs/agent-workflow.md" in dispatch_design
    assert "resumption contract" in guide.lower()
    assert "(schema-design.md#the-resumption-contract)" in guide


@pytest.mark.parametrize(
    "retired_symbol",
    [
        "client.mark_in_progress(",
        "client.mark_completed(",
        "client.mark_blocked(",
        "client.get_starter_prompt(",
        "client.add_followup_prompt(",
        "TaskStatus",
        "task.status_updates",
        "task.prompts",
    ],
)
def test_agent_workflow_guide_does_not_use_retired_v1_client_api(
    retired_symbol: str,
) -> None:
    """Every former quick-start symbol here raised or described deleted schema state."""
    guide = WORKFLOW_GUIDE.read_text(encoding="utf-8")
    assert retired_symbol not in guide


# ---------------------------------------------------------------------------
# The MCP documentation contract
#
# Two failure modes worth guarding. One is the docs going stale as the tool
# inventory changes. The other, worse, is overclaiming: a page that says the hook
# prevents direct writes, or that non-Codex clients get Codex protections, would
# give a reader confidence the system does not earn.
# ---------------------------------------------------------------------------
MCP_GUIDE = ROOT / "docs" / "mcp.md"
CLIENT_GUIDE = ROOT / "docs" / "mcp-clients.md"

ALL_TOOLS = [
    "projects_list",
    "tasks_list",
    "task_get",
    "tasks_search",
    "task_next",
    "task_create_draft",
    "task_create_ready",
    "task_claim",
    "task_release",
    "task_handoff",
    "task_close",
    "task_log_append",
    "task_update_content",
]


def flat(text: str) -> str:
    """Collapse whitespace so assertions are about wording, not line wrapping."""
    return " ".join(text.split())


def test_the_mcp_guide_names_every_published_tool() -> None:
    text = MCP_GUIDE.read_text(encoding="utf-8")
    for tool in ALL_TOOLS:
        assert tool in text, tool


def test_the_mcp_guide_does_not_restate_tool_schemas() -> None:
    """tools/list is authoritative; a copy here is a copy that goes stale."""
    text = MCP_GUIDE.read_text(encoding="utf-8")
    assert "that is the\nauthoritative reference" in text or "authoritative reference" in text
    assert '"inputSchema"' not in text


def test_the_docs_say_task_yaml_is_readable_generated_state() -> None:
    for path in ("docs/index.md", "docs/agent-workflow.md", "docs/task-schema.md"):
        assert "generated state" in (ROOT / path).read_text(encoding="utf-8"), path


def test_the_agent_workflow_guide_forbids_editing_yaml_on_a_tool_failure() -> None:
    text = flat((ROOT / "docs" / "agent-workflow.md").read_text(encoding="utf-8"))
    assert "A failing tool is not permission to edit YAML." in text
    assert "do not edit them" in text.lower()


def test_the_mcp_guide_states_what_each_layer_does_not_prevent() -> None:
    text = flat(MCP_GUIDE.read_text(encoding="utf-8"))
    assert "guardrail, not a security boundary" in text
    assert "cannot" in text and "which program wrote a file" in text
    for bypass in ("hosted tools", "obfuscation", "disabled hook"):
        assert bypass.split()[0] in text, bypass


def test_no_page_claims_non_codex_clients_get_the_codex_hook() -> None:
    """The single most tempting overclaim in this whole program."""
    text = flat(CLIENT_GUIDE.read_text(encoding="utf-8"))
    assert "No pre-tool hook" in text
    assert "it is a Codex plugin mechanism" in text.lower() or "Codex plugin mechanism" in text


def test_the_docs_do_not_present_unshipped_work_as_shipped() -> None:
    text = flat(MCP_GUIDE.read_text(encoding="utf-8"))
    assert "## Not shipped" in MCP_GUIDE.read_text(encoding="utf-8")
    assert "STDIO only" in text
    assert "Nothing subscribes to it yet" in text


def test_the_client_guide_covers_every_supported_client() -> None:
    text = CLIENT_GUIDE.read_text(encoding="utf-8")
    for client in ("## Codex", "## Claude", "## Gemini", "## Any other MCP client"):
        assert client in text, client


def test_the_client_guide_documents_a_posix_verification_path() -> None:
    """Windows is what the suite runs on; POSIX has to be documented instead."""
    text = CLIENT_GUIDE.read_text(encoding="utf-8")
    assert "## POSIX verification" in text
    assert "/bin/agentjobs" in text


def test_the_docs_are_reachable_from_the_index_and_the_nav() -> None:
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    for page in ("mcp.md", "mcp-clients.md"):
        assert page in index, page
        assert page in nav, page


def test_every_relative_link_in_the_mcp_pages_resolves() -> None:
    import re

    for page in (MCP_GUIDE, CLIENT_GUIDE):
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
            assert (page.parent / target).exists(), f"{page.name} -> {target}"


def test_the_readme_points_agents_at_mcp() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Agents connect over MCP" in text
    assert "docs/mcp.md" in text
