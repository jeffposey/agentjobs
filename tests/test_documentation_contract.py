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
