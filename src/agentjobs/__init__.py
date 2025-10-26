"""AgentJobs - Lightweight task management for AI agent workflows."""

from .client import TaskClient, TaskClientError  # noqa: F401
from .manager import TaskManager  # noqa: F401
from .models import (  # noqa: F401
    Branch,
    Deliverable,
    Dependency,
    ExternalLink,
    Issue,
    Phase,
    Priority,
    Prompt,
    Prompts,
    StatusUpdate,
    SuccessCriterion,
    Task,
    TaskStatus,
)
from .storage import TaskStorage  # noqa: F401
from .__version__ import __version__  # noqa: F401

__all__ = [
    "__version__",
    "Task",
    "TaskStatus",
    "Priority",
    "Phase",
    "SuccessCriterion",
    "Prompt",
    "Prompts",
    "StatusUpdate",
    "Deliverable",
    "Dependency",
    "ExternalLink",
    "Issue",
    "Branch",
    "TaskManager",
    "TaskStorage",
    "TaskClient",
    "TaskClientError",
]
