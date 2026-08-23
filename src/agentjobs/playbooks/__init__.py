"""Playbooks: reusable briefs for recurring work, stored per project in git.

``docs/playbooks-design.md`` is the design. This package holds the storage and contract
half (task-214) -- where a playbook lives, what its frontmatter must contain, how to
read one -- and, in :mod:`agentjobs.playbooks.run`, instantiation (task-215).

**Importing this package runs nothing and can start nothing.** ``run`` is deliberately
*not* imported here: it is the only module that reaches into ``agentjobs.dispatch``, and
leaving it out both keeps the two packages' imports one-directional (the dispatch layer
holds a :class:`~agentjobs.playbooks.pointer.PlaybookPointer`) and keeps every read
surface -- ``playbook list``, the GET routes, the MCP tool -- structurally unable to
reach an execution path. A caller that means to run one says so:

    from agentjobs.playbooks.run import run_playbook

Decision P10 keeps every run surface human-gated, and MCP has no run tool.
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
from .pointer import (
    HASH_ALGORITHM,
    SHORT_DIGEST_CHARS,
    PlaybookPointer,
    hash_text,
    pointer_for,
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
    "HASH_ALGORITHM",
    "SHORT_DIGEST_CHARS",
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
    "PlaybookPointer",
    "PlaybookRunTask",
    "PlaybookTarget",
    "UnknownPlaybookError",
    "hash_text",
    "install_references",
    "list_playbooks",
    "load_playbook",
    "load_reference",
    "parse_playbook",
    "playbook_payload",
    "playbooks_directory_name",
    "pointer_for",
    "read_playbook",
    "reference_names",
    "reference_text",
    "resolve_playbooks_dir",
    "split_frontmatter",
    "validate_playbook_name",
]
