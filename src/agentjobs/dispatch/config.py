"""Machine-local configuration for agent dispatch.

Dispatch turns an unauthenticated localhost HTTP API into remote code execution on this
machine. Every refusal path in ``docs/agent-dispatch-design.md`` is therefore a
configuration question, and this module answers all of them before anything can start a
process. Nothing here spawns; see task-070 for that.

The split follows ``projects.py``: *what a project is* is versioned with the project,
*what this machine will do about it* lives in ``~/.agentjobs/dispatch.yaml`` and is never
committed. That is not tidiness. If a runner's argv lived in a project's own
``.agentjobs/config.yaml``, cloning any repository would carry a "run this command on my
machine" payload that the project's config legitimises -- the one thing dispatch must
never allow (design section 6, gate 2).

Four gates, each independently sufficient to refuse a run:

1. the master switch (``enabled:``), with an absent file meaning off;
2. the runner must exist in this machine-local file;
3. per-project enablement, off until turned on;
4. the ``~/.agentjobs/DISPATCH_DISABLED`` sentinel, which overrides everything.

``assert_dispatch_permitted`` walks all four and is the single entry point later tasks
call. It returns the resolved runner or raises a typed error naming which gate refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import yaml

from agentjobs.projects import default_home

CONFIG_FILENAME = "dispatch.yaml"
SENTINEL_FILENAME = "DISPATCH_DISABLED"
SUPPORTED_VERSION = 1

PLACEHOLDERS = frozenset(
    {
        "prompt",
        "task_id",
        "project_id",
        "project_root",
        "run_id",
        "agent",
        "api_base",
    }
)
"""Substitutions available in a runner's argv (design section 3)."""

_PLACEHOLDER_TOKEN = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
"""Matches an identifier-shaped ``{token}`` only.

Deliberately narrow. A runner argv legitimately contains JSON -- ``--settings
{"permissions": ...}`` is how the permission allow-list is passed -- and treating every
brace as a placeholder would make that unrepresentable. Requiring an identifier inside
the braces still catches the typo case (``{propmt}``), which is the failure worth
catching, because it would otherwise reach the agent verbatim as part of its prompt.
"""


# ----- errors -----------------------------------------------------------------


class DispatchError(Exception):
    """Base class for every dispatch refusal.

    ``reason`` is a stable machine-readable code so the HTTP layer (task-071) can
    distinguish causes without matching on message text.
    """

    reason = "dispatch_error"


class DispatchConfigError(DispatchError):
    """The dispatch configuration file exists but cannot be trusted."""

    reason = "invalid_config"


class DispatchNotConfiguredError(DispatchError):
    """No ``~/.agentjobs/dispatch.yaml`` on this machine.

    Not an error condition for the machine -- a fresh install has no dispatch config and
    is expected not to. It is only an error for a caller that asked to dispatch.
    """

    reason = "not_configured"


class DispatchDisabledError(DispatchError):
    """The master switch is off."""

    reason = "disabled"


class DispatchSentinelError(DispatchError):
    """``~/.agentjobs/DISPATCH_DISABLED`` exists.

    The panic button. File-based on purpose: it works when the server is wedged, and it
    can be created by ``touch``, by Explorer, or by any editor.
    """

    reason = "sentinel"


class ProjectNotEnabledError(DispatchError):
    """This project has not been enabled for dispatch on this machine."""

    reason = "project_not_enabled"


class UnknownRunnerError(DispatchError):
    """The project names a runner that this machine does not define."""

    reason = "unknown_runner"


class PlaceholderError(DispatchError):
    """A runner's argv references a placeholder that is unknown or unsupplied."""

    reason = "bad_placeholder"


# ----- model ------------------------------------------------------------------


class RunnerMode(str, Enum):
    """Which lifecycle a runner's process has (design section 4).

    ``session`` starts a steerable background session and returns immediately;
    ``batch`` runs to completion under a supervisor and has a spend ceiling and a
    wall-clock timeout. A runner declares its own mode.
    """

    SESSION = "session"
    BATCH = "batch"


class Posture(str, Enum):
    """What a dispatched agent may do once running (design section 4, task-076)."""

    READ_ONLY = "read_only"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


@dataclass(frozen=True)
class DispatchRunner:
    """A named recipe for starting an agent."""

    name: str
    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    mode: RunnerMode = RunnerMode.BATCH

    def render(self, values: Mapping[str, str]) -> List[str]:
        """Substitute ``values`` into this runner's argv, per element and literally."""
        return substitute_argv(self.argv, values)


@dataclass(frozen=True)
class ProjectDispatchSettings:
    """Whether and how one project may dispatch on this machine."""

    project_id: str
    enabled: bool = False
    runner: Optional[str] = None
    require_clean_tree: bool = True
    auto_dispatch: bool = False
    posture: Posture = Posture.SUPERVISED


@dataclass(frozen=True)
class AutoDispatchLimits:
    """Budget caps. These bind auto-dispatch only (design section 7, D3).

    A human clicking Dispatch repeatedly is a decision, not a malfunction; refusing it
    would be the tool second-guessing its owner about his own money.
    """

    per_task_per_day: int = 3
    per_task_lifetime: int = 10
    cooldown_seconds: int = 60


@dataclass(frozen=True)
class DispatchLimits:
    """Safety caps. Unlike the auto budget, these bind every run including manual."""

    max_concurrent_runs: int = 1
    run_timeout_seconds: int = 1800
    session_stale_seconds: int = 3600
    auto: AutoDispatchLimits = field(default_factory=AutoDispatchLimits)


@dataclass(frozen=True)
class DispatchConfig:
    """The parsed contents of ``~/.agentjobs/dispatch.yaml``."""

    version: int = SUPPORTED_VERSION
    enabled: bool = False
    runners: Dict[str, DispatchRunner] = field(default_factory=dict)
    projects: Dict[str, ProjectDispatchSettings] = field(default_factory=dict)
    limits: DispatchLimits = field(default_factory=DispatchLimits)
    path: Optional[Path] = None

    def project(self, project_id: str) -> ProjectDispatchSettings:
        """Settings for ``project_id``, defaulted (and therefore disabled) if absent."""
        return self.projects.get(project_id) or ProjectDispatchSettings(project_id=project_id)


@dataclass(frozen=True)
class DispatchResolution:
    """What ``assert_dispatch_permitted`` returns when every gate is open."""

    project_id: str
    runner: DispatchRunner
    settings: ProjectDispatchSettings
    limits: DispatchLimits
    config: DispatchConfig


# ----- locations --------------------------------------------------------------


def dispatch_config_path(home: Optional[Path] = None) -> Path:
    """Location of the machine-local dispatch configuration."""
    return _home(home) / CONFIG_FILENAME


def sentinel_path(home: Optional[Path] = None) -> Path:
    """Location of the kill-switch sentinel file."""
    return _home(home) / SENTINEL_FILENAME


def sentinel_active(home: Optional[Path] = None) -> bool:
    """True when the sentinel refuses all new runs."""
    return sentinel_path(home).exists()


def _home(home: Optional[Path]) -> Path:
    """Resolve the AgentJobs home, honouring ``AGENTJOBS_HOME`` via ``projects.py``."""
    return Path(home).expanduser().resolve() if home else default_home()


# ----- loading ----------------------------------------------------------------


def load_dispatch_config(home: Optional[Path] = None) -> Optional[DispatchConfig]:
    """Read the dispatch configuration, or return ``None`` when there is none.

    Read from disk on every call, never cached. A stale runner after editing the file is
    the kind of failure that costs an hour to diagnose, and this file is edited by hand
    far more often than it is read.
    """
    path = dispatch_config_path(home)
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DispatchConfigError(f"Cannot read dispatch config at {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise DispatchConfigError(f"Invalid dispatch config at {path}: expected a mapping.")
    return _parse(loaded, path)


def _parse(raw: dict, path: Path) -> DispatchConfig:
    """Turn a raw mapping into a validated ``DispatchConfig``."""
    version = raw.get("version", SUPPORTED_VERSION)
    if version != SUPPORTED_VERSION:
        raise DispatchConfigError(
            f"Unsupported dispatch config version {version!r} at {path}: "
            f"this build understands version {SUPPORTED_VERSION}."
        )

    runners = {
        name: _parse_runner(name, value, path)
        for name, value in _mapping(raw.get("runners"), "runners", path).items()
    }
    projects = {
        project_id: _parse_project(project_id, value, path)
        for project_id, value in _mapping(raw.get("projects"), "projects", path).items()
    }

    # A project naming a runner this machine does not define is deliberately *not* a
    # load error. It is a refusal for that project only, raised by
    # assert_dispatch_permitted as UnknownRunnerError -- so one bad entry cannot take
    # the whole file, and every other project, down with it.

    return DispatchConfig(
        version=version,
        enabled=_bool(raw.get("enabled"), "enabled", path, default=False),
        runners=runners,
        projects=projects,
        limits=_parse_limits(_mapping(raw.get("limits"), "limits", path), path),
        path=path,
    )


def _parse_runner(name: str, raw: object, path: Path) -> DispatchRunner:
    """Validate one runner definition."""
    where = f"runners.{name}"
    mapping = _mapping(raw, where, path)

    argv = mapping.get("argv")
    if not isinstance(argv, list) or not argv:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.argv must be a non-empty list."
        )
    if not all(isinstance(element, str) for element in argv):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: every element of {where}.argv must be a "
            "string. argv is a list because there is no shell anywhere in dispatch."
        )
    validate_argv_template(argv, where=f"{where}.argv", path=path)

    env_raw = _mapping(mapping.get("env"), f"{where}.env", path)
    env = {}
    for key, value in env_raw.items():
        if not isinstance(value, str):
            raise DispatchConfigError(
                f"Invalid dispatch config at {path}: {where}.env.{key} must be a string."
            )
        env[key] = value

    mode_raw = mapping.get("mode", RunnerMode.BATCH.value)
    try:
        mode = RunnerMode(mode_raw)
    except ValueError as exc:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.mode must be one of "
            f"{_values(RunnerMode)}, not {mode_raw!r}."
        ) from exc

    return DispatchRunner(name=name, argv=list(argv), env=env, mode=mode)


def _parse_project(project_id: str, raw: object, path: Path) -> ProjectDispatchSettings:
    """Validate one project's dispatch settings."""
    where = f"projects.{project_id}"
    mapping = _mapping(raw, where, path)

    runner = mapping.get("runner")
    if runner is not None and not isinstance(runner, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.runner must be a string."
        )

    posture_raw = mapping.get("posture", Posture.SUPERVISED.value)
    try:
        posture = Posture(posture_raw)
    except ValueError as exc:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.posture must be one of "
            f"{_values(Posture)}, not {posture_raw!r}."
        ) from exc

    return ProjectDispatchSettings(
        project_id=project_id,
        enabled=_bool(mapping.get("enabled"), f"{where}.enabled", path, default=False),
        runner=runner,
        require_clean_tree=_bool(
            mapping.get("require_clean_tree"), f"{where}.require_clean_tree", path, default=True
        ),
        auto_dispatch=_bool(
            mapping.get("auto_dispatch"), f"{where}.auto_dispatch", path, default=False
        ),
        posture=posture,
    )


def _parse_limits(raw: Mapping[str, object], path: Path) -> DispatchLimits:
    """Validate the limits block, defaulting anything absent."""
    defaults = DispatchLimits()
    auto_defaults = AutoDispatchLimits()
    auto_raw = _mapping(raw.get("auto"), "limits.auto", path)
    return DispatchLimits(
        max_concurrent_runs=_positive_int(
            raw.get("max_concurrent_runs"),
            "limits.max_concurrent_runs",
            path,
            defaults.max_concurrent_runs,
        ),
        run_timeout_seconds=_positive_int(
            raw.get("run_timeout_seconds"),
            "limits.run_timeout_seconds",
            path,
            defaults.run_timeout_seconds,
        ),
        session_stale_seconds=_positive_int(
            raw.get("session_stale_seconds"),
            "limits.session_stale_seconds",
            path,
            defaults.session_stale_seconds,
        ),
        auto=AutoDispatchLimits(
            per_task_per_day=_positive_int(
                auto_raw.get("per_task_per_day"),
                "limits.auto.per_task_per_day",
                path,
                auto_defaults.per_task_per_day,
            ),
            per_task_lifetime=_positive_int(
                auto_raw.get("per_task_lifetime"),
                "limits.auto.per_task_lifetime",
                path,
                auto_defaults.per_task_lifetime,
            ),
            cooldown_seconds=_positive_int(
                auto_raw.get("cooldown_seconds"),
                "limits.auto.cooldown_seconds",
                path,
                auto_defaults.cooldown_seconds,
            ),
        ),
    )


def _mapping(value: object, where: str, path: Path) -> Dict[str, object]:
    """Coerce an optional YAML block to a mapping, or raise naming where it was."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DispatchConfigError(f"Invalid dispatch config at {path}: {where} must be a mapping.")
    return {str(key): item for key, item in value.items()}


def _bool(value: object, where: str, path: Path, *, default: bool) -> bool:
    """Read a strict boolean. ``yes``/``1`` are not booleans and are refused."""
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where} must be true or false."
        )
    return value


def _positive_int(value: object, where: str, path: Path, default: int) -> int:
    """Read a positive integer limit, defaulting when absent."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where} must be a positive integer."
        )
    return value


def _known(runners: Mapping[str, object]) -> str:
    """Comma-separated runner names for an error message."""
    return ", ".join(sorted(runners)) or "none defined"


def _values(enum_class: type) -> str:
    """Comma-separated values of a string enum, for error messages."""
    return ", ".join(member.value for member in enum_class)  # type: ignore[attr-defined]


# ----- substitution -----------------------------------------------------------


def validate_argv_template(
    argv: Iterable[str], *, where: str = "argv", path: Optional[Path] = None
) -> None:
    """Refuse an argv template referencing a placeholder dispatch cannot supply.

    Checked at load rather than at spawn so a typo is a configuration error rather than
    a literal ``{propmt}`` handed to an agent as part of its instructions.
    """
    for element in argv:
        for match in _PLACEHOLDER_TOKEN.finditer(element):
            name = match.group(1)
            if name not in PLACEHOLDERS:
                location = f" at {path}" if path else ""
                raise DispatchConfigError(
                    f"Invalid dispatch config{location}: {where} references unknown "
                    f"placeholder {{{name}}}. Known placeholders: "
                    f"{', '.join(sorted(PLACEHOLDERS))}."
                )


def substitute_argv(argv: Sequence[str], values: Mapping[str, str]) -> List[str]:
    """Substitute placeholders into an argv template, per element and literally.

    The result is a list handed to ``subprocess`` as a list. It is never joined into a
    command string, and there is no ``shell=True`` anywhere in this subsystem, so a
    prompt containing quotes, semicolons, backticks or newlines stays exactly one
    argument no matter what it says.

    Not ``str.format``: a prompt is arbitrary text that routinely contains braces, and
    ``format`` would either raise on it or interpolate an attacker-chosen field.
    """
    rendered: List[str] = []
    for element in argv:
        rendered.append(_PLACEHOLDER_TOKEN.sub(lambda m: _value_for(m.group(1), values), element))
    return rendered


def _value_for(name: str, values: Mapping[str, str]) -> str:
    """Resolve one placeholder, or raise naming what was missing."""
    if name not in PLACEHOLDERS:
        raise PlaceholderError(
            f"Unknown placeholder {{{name}}}. Known placeholders: "
            f"{', '.join(sorted(PLACEHOLDERS))}."
        )
    if name not in values:
        raise PlaceholderError(f"No value supplied for placeholder {{{name}}}.")
    return str(values[name])


# ----- the gates --------------------------------------------------------------


def assert_dispatch_permitted(project_id: str, home: Optional[Path] = None) -> DispatchResolution:
    """Walk every dispatch gate for ``project_id`` and resolve its runner.

    The single entry point the API, the CLI and the supervisor all call. Raises a
    ``DispatchError`` subclass naming the gate that refused; returns the resolved runner
    only when all four are open.

    The sentinel is checked first and re-checked immediately before every spawn by the
    caller: this function proves dispatch was permitted at the moment it was asked, not
    for the lifetime of the answer.
    """
    if sentinel_active(home):
        raise DispatchSentinelError(
            f"Dispatch is disabled by {sentinel_path(home)}. Delete that file to re-enable."
        )

    config = load_dispatch_config(home)
    if config is None:
        raise DispatchNotConfiguredError(
            f"Dispatch is not configured on this machine: {dispatch_config_path(home)} "
            "does not exist. Create it and define a runner before enabling a project."
        )

    if not config.enabled:
        raise DispatchDisabledError(
            f"Dispatch is switched off: set 'enabled: true' in {config.path}."
        )

    settings = config.project(project_id)
    if not settings.enabled:
        raise ProjectNotEnabledError(
            f"Project {project_id!r} is not enabled for dispatch. "
            f"Run 'agentjobs dispatch enable {project_id}'."
        )

    if not settings.runner:
        raise UnknownRunnerError(
            f"Project {project_id!r} is enabled for dispatch but names no runner. "
            f"Set projects.{project_id}.runner in {config.path}."
        )

    runner = config.runners.get(settings.runner)
    if runner is None:
        raise UnknownRunnerError(
            f"Project {project_id!r} names runner {settings.runner!r}, which is not "
            f"defined in {config.path}. Known runners: {_known(config.runners)}."
        )

    return DispatchResolution(
        project_id=project_id,
        runner=runner,
        settings=settings,
        limits=config.limits,
        config=config,
    )


# ----- mutation ---------------------------------------------------------------


def set_project_enabled(
    project_id: str,
    enabled: bool,
    *,
    runner: Optional[str] = None,
    home: Optional[Path] = None,
) -> ProjectDispatchSettings:
    """Turn dispatch on or off for one project, and return its resulting settings.

    Enablement is the only part of this file a browser or a CLI may write. Runners are
    never created here: a project can only be pointed at a command that a human already
    wrote into this machine's config by hand, which is what keeps the reachable
    execution surface exactly as wide as that file says (design section 6, gate 3).

    The raw mapping is edited in place rather than being round-tripped through the
    dataclasses, so keys this build does not understand survive the write.
    """
    path = dispatch_config_path(home)
    if not path.is_file():
        raise DispatchNotConfiguredError(
            f"Dispatch is not configured on this machine: {path} does not exist. "
            "Create it and define a runner before enabling a project."
        )

    config = load_dispatch_config(home)
    assert config is not None  # load_dispatch_config only returns None when absent

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DispatchConfigError(f"Invalid dispatch config at {path}: expected a mapping.")

    projects = raw.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise DispatchConfigError(f"Invalid dispatch config at {path}: projects must be a mapping.")
    entry = projects.setdefault(project_id, {})
    if not isinstance(entry, dict):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: projects.{project_id} must be a mapping."
        )

    if enabled:
        chosen = _runner_for_enable(project_id, runner, entry.get("runner"), config, path)
        entry["runner"] = chosen
    elif runner is not None:
        raise DispatchError("--runner is meaningless when disabling a project.")

    entry["enabled"] = enabled

    path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )

    updated = load_dispatch_config(home)
    assert updated is not None
    return updated.project(project_id)


def _runner_for_enable(
    project_id: str,
    requested: Optional[str],
    existing: object,
    config: DispatchConfig,
    path: Path,
) -> str:
    """Pick the runner to enable a project with, refusing one that does not exist."""
    if requested is not None:
        if requested not in config.runners:
            raise UnknownRunnerError(
                f"No runner named {requested!r} is defined in {path}. "
                f"Known runners: {_known(config.runners)}. Runners are written by hand, "
                "on this machine, and never by a project or the GUI."
            )
        return requested

    if isinstance(existing, str):
        if existing not in config.runners:
            raise UnknownRunnerError(
                f"Project {project_id!r} already names runner {existing!r}, which is not "
                f"defined in {path}. Known runners: {_known(config.runners)}."
            )
        return existing

    if len(config.runners) == 1:
        return next(iter(config.runners))

    raise UnknownRunnerError(
        f"Cannot tell which runner {project_id!r} should use. "
        f"Pass --runner. Known runners: {_known(config.runners)}."
    )
