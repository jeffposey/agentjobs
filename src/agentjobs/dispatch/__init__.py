"""Agent dispatch: turning a human decision into a running agent.

See ``docs/agent-dispatch-design.md``. This package currently holds only the
configuration layer (task-068); nothing here starts a process.
"""

from __future__ import annotations

from agentjobs.dispatch.config import (
    DispatchConfig,
    DispatchConfigError,
    DispatchDisabledError,
    DispatchError,
    DispatchLimits,
    DispatchNotConfiguredError,
    DispatchResolution,
    DispatchRunner,
    DispatchSentinelError,
    PlaceholderError,
    Posture,
    ProjectDispatchSettings,
    ProjectNotEnabledError,
    RunnerMode,
    UnknownRunnerError,
    assert_dispatch_permitted,
    dispatch_config_path,
    load_dispatch_config,
    sentinel_active,
    sentinel_path,
    set_project_enabled,
    substitute_argv,
)

__all__ = [
    "DispatchConfig",
    "DispatchConfigError",
    "DispatchDisabledError",
    "DispatchError",
    "DispatchLimits",
    "DispatchNotConfiguredError",
    "DispatchResolution",
    "DispatchRunner",
    "DispatchSentinelError",
    "PlaceholderError",
    "Posture",
    "ProjectDispatchSettings",
    "ProjectNotEnabledError",
    "RunnerMode",
    "UnknownRunnerError",
    "assert_dispatch_permitted",
    "dispatch_config_path",
    "load_dispatch_config",
    "sentinel_active",
    "sentinel_path",
    "set_project_enabled",
    "substitute_argv",
]
