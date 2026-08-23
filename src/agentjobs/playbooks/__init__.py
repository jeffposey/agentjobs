"""Playbooks: reusable briefs for recurring work, stored per project in git.

``docs/playbooks-design.md`` is the design. This package is its storage and contract
half (task-214): where a playbook lives, what its frontmatter must contain, and how to
read one. **Nothing here runs a playbook** -- instantiation and dispatch are task-215,
and decision P10 keeps every run surface human-gated.
"""

from .library import (
    DEFAULT_PLAYBOOKS_DIRECTORY,
    PLAYBOOKS_DIRECTORY_KEY,
    InstallResult,
    PlaybookListing,
    UnknownPlaybookError,
    install_references,
    list_playbooks,
    load_reference,
    playbooks_directory_name,
    read_playbook,
    reference_names,
    reference_text,
    resolve_playbooks_dir,
    validate_playbook_name,
)
from .model import (
    MANAGER_VERBS,
    PLAYBOOK_SUFFIX,
    Playbook,
    PlaybookAcceptance,
    PlaybookContract,
    PlaybookDifficulty,
    PlaybookError,
    PlaybookFinding,
    PlaybookGate,
    PlaybookRunTask,
    PlaybookTarget,
    load_playbook,
    parse_playbook,
    playbook_payload,
    split_frontmatter,
)

__all__ = [
    "DEFAULT_PLAYBOOKS_DIRECTORY",
    "InstallResult",
    "MANAGER_VERBS",
    "PLAYBOOKS_DIRECTORY_KEY",
    "PLAYBOOK_SUFFIX",
    "Playbook",
    "PlaybookAcceptance",
    "PlaybookContract",
    "PlaybookDifficulty",
    "PlaybookError",
    "PlaybookFinding",
    "PlaybookGate",
    "PlaybookListing",
    "PlaybookRunTask",
    "PlaybookTarget",
    "UnknownPlaybookError",
    "install_references",
    "list_playbooks",
    "load_playbook",
    "load_reference",
    "parse_playbook",
    "playbook_payload",
    "playbooks_directory_name",
    "read_playbook",
    "reference_names",
    "reference_text",
    "resolve_playbooks_dir",
    "split_frontmatter",
    "validate_playbook_name",
]
