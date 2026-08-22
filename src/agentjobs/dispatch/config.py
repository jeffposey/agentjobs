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

**Runner groups (task-177)** sit on top of the flat ``runners:`` map: a group is an
ordered list of runners that are interchangeable for one kind of work, a dispatch may
name one, and the first member that can actually run is the one that runs. Everything
about them is additive -- a config with no ``runner_groups:`` block behaves exactly as
it did, resolves through ``projects.<id>.runner``, and logs nothing new. Selection
happens after all four gates, never around them, which is why it lives inside
``assert_dispatch_permitted`` rather than beside it.
"""

from __future__ import annotations

import re
import shutil
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


class UnknownGroupError(DispatchError):
    """A dispatch, a project, or the machine default names a group nobody defined.

    Separate from ``UnknownRunnerError`` because the fix is different: one says "point
    this at a runner you wrote", the other says "point this at a group you wrote", and a
    caller that cannot tell them apart tells its user to edit the wrong block.
    """

    reason = "unknown_group"


class NoEligibleRunnerError(DispatchError):
    """A group resolved, and every member of it was passed over.

    Deliberately not a fallback to the project's plain runner. A group is the operator's
    statement about *which runners are interchangeable for this kind of work*; reaching
    outside it would run a model the requester did not ask for, at a cost they did not
    choose, which is the failure the group layer exists to prevent. The message names
    every candidate and why each was skipped, so the refusal is actionable.
    """

    reason = "no_eligible_runner"


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
    """What a dispatched agent may do once running (design section 4, task-076).

    ``AUTO`` is the default, per task-020. ``SUPERVISED`` held that role until
    2026-08-19, when a real dispatch parked on its first shell command -- ``ls`` on the
    repository's own docs directory -- because the allow-list covers nine prefixes and
    nothing else. A ``--bg`` session has no terminal to answer with, so the run sat at
    ``state: blocked`` until it was cancelled. Supervised remains right for a run
    somebody is actually watching; it is no longer what an unattended one gets.
    """

    READ_ONLY = "read_only"
    AUTO = "auto"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


@dataclass(frozen=True)
class DispatchRunner:
    """A named recipe for starting an agent."""

    name: str
    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    mode: RunnerMode = RunnerMode.BATCH
    actor: Optional[str] = None
    """Which configured actor this runner writes as. Defaults to the runner's name.

    These are two different things, and conflating them was a real defect. A runner name
    describes an *invocation* -- ``claude-session``, ``claude-opus-max`` -- and operators
    name them that way. An actor is an identity in the project's ``actors:`` vocabulary.
    Dispatch claimed tasks and wrote log entries under the runner name, which the
    task-write API then refused as unknown, so a dispatched agent could not log progress
    under the identity that owned its own task.
    """

    @property
    def actor_id(self) -> str:
        """The identity this runner acts as. Never empty."""
        return self.actor or self.name

    def render(self, values: Mapping[str, str]) -> List[str]:
        """Substitute ``values`` into this runner's argv, per element and literally."""
        return substitute_argv(self.argv, values)


@dataclass(frozen=True)
class RunnerGroupMember:
    """One runner's membership in a group: which runner, and whether it is in play."""

    runner: str
    enabled: bool = True
    note: Optional[str] = None
    """Free text for the human reading the file. Never parsed, never acted on.

    It exists so ``enabled: false`` can say *why* -- "no API key on this machine yet",
    "waiting on the org to approve the spend" -- beside the flag rather than in someone's
    memory. A disabled member with no explanation is indistinguishable from a mistake.
    """


@dataclass(frozen=True)
class RunnerGroup:
    """An ordered list of runners that are interchangeable for one kind of work.

    Order is the declaration order in the file and is the whole preference mechanism:
    the first member that can actually run is the one that runs. There is no scoring, no
    weighting, and nothing consulted over the network -- see the task-177 decision on
    what the installed CLIs do and do not expose.
    """

    name: str
    members: List[RunnerGroupMember] = field(default_factory=list)
    description: Optional[str] = None


class SelectionSource(str, Enum):
    """Which rung of the precedence ladder decided what runs (design section 4)."""

    DISPATCH = "dispatch"
    """A group named on this one dispatch. The narrowest thing that can win today."""

    PROJECT = "project"
    """``projects.<id>.group``."""

    MACHINE = "machine"
    """``default_group:``, the machine-wide fallback group."""

    PROJECT_RUNNER = "project_runner"
    """``projects.<id>.runner`` -- today's behaviour, and the last rung of the ladder."""


class SkipReason(str, Enum):
    """Why a group member was passed over. Recorded per candidate, in the task log."""

    DISABLED = "disabled"
    """``enabled: false``. A hand edit is the only thing that changes it."""

    UNDEFINED_RUNNER = "undefined_runner"
    """The member names a runner absent from ``runners:``."""

    EXECUTABLE_NOT_FOUND = "executable_not_found"
    """``argv[0]`` does not resolve on PATH, so starting it would fail with WinError 2."""


@dataclass(frozen=True)
class RunnerCandidate:
    """One member of a group, and what the selector concluded about it."""

    runner: str
    eligible: bool
    skipped_because: Optional[SkipReason] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class RunnerSelection:
    """The chosen runner and the complete account of how it was chosen.

    Carried on the resolution and copied into the ``dispatch`` log entry, because the
    thing that makes a group safe is not the selection rule but the ability to answer
    "why did this run on that model" three weeks later from the git-tracked record.
    """

    runner: DispatchRunner
    source: SelectionSource
    group: Optional[str] = None
    candidates: List[RunnerCandidate] = field(default_factory=list)

    @property
    def from_group(self) -> bool:
        """True when a group participated, so a flat setup logs nothing new."""
        return self.group is not None


@dataclass(frozen=True)
class ProjectDispatchSettings:
    """Whether and how one project may dispatch on this machine."""

    project_id: str
    enabled: bool = False
    runner: Optional[str] = None
    group: Optional[str] = None
    require_clean_tree: bool = True
    auto_dispatch: bool = False
    posture: Posture = Posture.AUTO
    resume_sessions: bool = True
    """Whether dispatching a task resumes its previous session instead of starting cold.

    On by default: a task's second run is almost always the post-approval merge, and a
    cold agent spends about eleven minutes rediscovering the branch and worktree the
    first one already had (task-234). Resuming keeps that context, and every doubt --
    no previous session, a conversation the session manager no longer lists, an argv
    this cannot rewrite -- falls back to a cold start rather than to a failure.

    The switch exists because the failure mode is a *confident* agent rather than a
    broken one. A resumed conversation acts on what it remembers; if that ever starts
    producing agents whose memory disagrees with the tree, this turns it off without a
    code change. Nothing else about the run changes when it is off.
    """


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
    runner_groups: Dict[str, RunnerGroup] = field(default_factory=dict)
    default_group: Optional[str] = None
    projects: Dict[str, ProjectDispatchSettings] = field(default_factory=dict)
    limits: DispatchLimits = field(default_factory=DispatchLimits)
    api_base: Optional[str] = None
    """Where this machine serves AgentJobs, for a dispatch with no request to ask.

    Machine-local because the answer is a property of this machine and not of any
    project -- the same repository dispatched from a laptop and from a server is told a
    different address, and only the machine knows which. A dispatch over HTTP does not
    need this: the endpoint knows the socket it answered on and passes that instead.
    See ``dispatch/address.py`` for the full precedence.
    """
    path: Optional[Path] = None

    def project(self, project_id: str) -> ProjectDispatchSettings:
        """Settings for ``project_id``, defaulted (and therefore disabled) if absent."""
        return self.projects.get(project_id) or ProjectDispatchSettings(project_id=project_id)


@dataclass(frozen=True)
class DispatchResolution:
    """What ``assert_dispatch_permitted`` returns when every gate is open.

    ``runner`` is the runner that will start. ``selection`` is the account of how it was
    arrived at -- identical information for a flat config, where the account is one rung
    long and nothing is logged about it.
    """

    project_id: str
    runner: DispatchRunner
    settings: ProjectDispatchSettings
    limits: DispatchLimits
    config: DispatchConfig
    selection: Optional[RunnerSelection] = None
    """How ``runner`` was chosen, or ``None`` when no group participated.

    ``None`` rather than a one-rung selection on purpose: it is the flag that keeps a
    flat config's ``dispatch`` log entry byte-identical to what it was before groups
    existed. Someone who never wants a group should not learn from their task files that
    groups exist.
    """


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
    runner_groups = {
        name: _parse_group(name, value, path)
        for name, value in _mapping(raw.get("runner_groups"), "runner_groups", path).items()
    }
    default_group = raw.get("default_group")
    if default_group is not None and not isinstance(default_group, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: default_group must be a string naming "
            "one of runner_groups."
        )
    projects = {
        project_id: _parse_project(project_id, value, path)
        for project_id, value in _mapping(raw.get("projects"), "projects", path).items()
    }

    # A project naming a runner this machine does not define is deliberately *not* a
    # load error. It is a refusal for that project only, raised by
    # assert_dispatch_permitted as UnknownRunnerError -- so one bad entry cannot take
    # the whole file, and every other project, down with it.
    #
    # The same holds for a group member naming an undefined runner, and for a group name
    # nobody defined. Deferring those is what makes "write the second option now, enable
    # it once you have set it up" a legal, working state rather than a file that will not
    # load. The selector reports each one against the member it came from.

    return DispatchConfig(
        version=version,
        enabled=_bool(raw.get("enabled"), "enabled", path, default=False),
        runners=runners,
        runner_groups=runner_groups,
        default_group=default_group or None,
        projects=projects,
        limits=_parse_limits(_mapping(raw.get("limits"), "limits", path), path),
        api_base=_parse_api_base(raw.get("api_base"), path),
        path=path,
    )


def _parse_api_base(raw: object, path: Path) -> Optional[str]:
    """Validate the machine's declared AgentJobs address, if it declared one."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: api_base must be a non-empty string "
            "naming where AgentJobs serves on this machine, e.g. http://localhost:8876."
        )
    return raw.strip().rstrip("/")


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

    actor_raw = mapping.get("actor")
    if actor_raw is not None and not isinstance(actor_raw, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.actor must be a string naming "
            f"an actor in the project's 'actors:', not {actor_raw!r}."
        )

    return DispatchRunner(name=name, argv=list(argv), env=env, mode=mode, actor=actor_raw or None)


def _parse_group(name: str, raw: object, path: Path) -> RunnerGroup:
    """Validate one runner group: an ordered list of members, and nothing clever.

    A member may be written as a bare string (``- claude-opus``) or as a mapping
    (``- runner: claude-opus`` with ``enabled`` and ``note``). Both are accepted because
    the short form is what a group of already-working runners looks like, and forcing a
    mapping on it would make the common case the ugly one.
    """
    where = f"runner_groups.{name}"
    mapping = _mapping(raw, where, path)

    description = mapping.get("description")
    if description is not None and not isinstance(description, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.description must be a string."
        )

    raw_members = mapping.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.members must be a non-empty "
            "list. A group with no members can never dispatch, which is a slower way of "
            "saying the group should not exist."
        )

    members: List[RunnerGroupMember] = []
    seen: set[str] = set()
    for index, element in enumerate(raw_members):
        member = _parse_group_member(element, f"{where}.members[{index}]", path)
        if member.runner in seen:
            raise DispatchConfigError(
                f"Invalid dispatch config at {path}: {where} lists runner "
                f"{member.runner!r} twice. Order is the preference, so a duplicate has "
                "no meaning that the first mention does not already have."
            )
        seen.add(member.runner)
        members.append(member)

    return RunnerGroup(name=name, members=members, description=description)


def _parse_group_member(raw: object, where: str, path: Path) -> RunnerGroupMember:
    """Validate one group member in either the bare-string or the mapping form."""
    if isinstance(raw, str):
        return RunnerGroupMember(runner=raw)

    mapping = _mapping(raw, where, path)
    runner = mapping.get("runner")
    if not isinstance(runner, str) or not runner:
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.runner must name a runner "
            "defined under 'runners:'."
        )

    note = mapping.get("note")
    if note is not None and not isinstance(note, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.note must be a string."
        )

    return RunnerGroupMember(
        runner=runner,
        enabled=_bool(mapping.get("enabled"), f"{where}.enabled", path, default=True),
        note=note,
    )


def _parse_project(project_id: str, raw: object, path: Path) -> ProjectDispatchSettings:
    """Validate one project's dispatch settings."""
    where = f"projects.{project_id}"
    mapping = _mapping(raw, where, path)

    runner = mapping.get("runner")
    if runner is not None and not isinstance(runner, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.runner must be a string."
        )

    group = mapping.get("group")
    if group is not None and not isinstance(group, str):
        raise DispatchConfigError(
            f"Invalid dispatch config at {path}: {where}.group must be a string naming "
            "one of runner_groups."
        )

    posture_raw = mapping.get("posture", Posture.AUTO.value)
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
        group=group,
        require_clean_tree=_bool(
            mapping.get("require_clean_tree"), f"{where}.require_clean_tree", path, default=True
        ),
        auto_dispatch=_bool(
            mapping.get("auto_dispatch"), f"{where}.auto_dispatch", path, default=False
        ),
        posture=posture,
        resume_sessions=_bool(
            mapping.get("resume_sessions"), f"{where}.resume_sessions", path, default=True
        ),
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


# ----- group selection --------------------------------------------------------


def executable_available(argv: Sequence[str]) -> bool:
    """Whether this runner's program can actually be started on this machine.

    The one selection input that is about the world rather than about the file, and it
    is deliberately the cheapest such input there is: a PATH scan, no subprocess, no
    network, no clock. A member whose CLI is not installed is the ordinary case this
    exists for -- a group that lists a second vendor on a machine where only the first
    is installed should cost a skip, not a ``WinError 2`` after the task has been
    claimed.

    A templated ``argv[0]`` is treated as available. Nothing can know what it will
    become, and guessing "unavailable" would silently remove a working runner from every
    group it is in.
    """
    if not argv:
        return False
    program = argv[0]
    if "{" in program:
        return True
    return shutil.which(program) is not None


def select_runner(
    group: RunnerGroup,
    runners: Mapping[str, DispatchRunner],
    *,
    source: SelectionSource,
) -> RunnerSelection:
    """Choose the first member of ``group`` that can run, and account for the rest.

    Deterministic by construction: the inputs are the file's declared order and three
    local predicates, so the same config selects the same member every time. Candidates
    after the winner are still listed, marked eligible, and not evaluated further --
    "considered and not reached" is a different fact from "considered and rejected", and
    the log entry should not blur them.

    Raises ``NoEligibleRunnerError`` when every member was passed over. See that class
    for why this does not fall back to the project's plain runner.
    """
    candidates: List[RunnerCandidate] = []
    chosen: Optional[DispatchRunner] = None

    for member in group.members:
        if chosen is not None:
            candidates.append(RunnerCandidate(runner=member.runner, eligible=True))
            continue

        definition = runners.get(member.runner)
        if not member.enabled:
            candidates.append(
                RunnerCandidate(
                    runner=member.runner,
                    eligible=False,
                    skipped_because=SkipReason.DISABLED,
                    detail=member.note,
                )
            )
        elif definition is None:
            candidates.append(
                RunnerCandidate(
                    runner=member.runner,
                    eligible=False,
                    skipped_because=SkipReason.UNDEFINED_RUNNER,
                    detail=f"No runner named {member.runner!r} under 'runners:'.",
                )
            )
        elif not executable_available(definition.argv):
            candidates.append(
                RunnerCandidate(
                    runner=member.runner,
                    eligible=False,
                    skipped_because=SkipReason.EXECUTABLE_NOT_FOUND,
                    detail=f"{definition.argv[0]!r} is not on PATH.",
                )
            )
        else:
            chosen = definition
            candidates.append(RunnerCandidate(runner=member.runner, eligible=True))

    if chosen is None:
        raise NoEligibleRunnerError(
            f"Runner group {group.name!r} has no member that can run: "
            f"{_why_skipped(candidates)}. Enable a member by hand, install the CLI it "
            "needs, or dispatch against a different group -- nothing outside the group "
            "is substituted for it."
        )

    return RunnerSelection(runner=chosen, source=source, group=group.name, candidates=candidates)


def _why_skipped(candidates: Sequence[RunnerCandidate]) -> str:
    """Each skipped candidate and its reason, for the refusal message."""
    return "; ".join(
        f"{candidate.runner} ({candidate.skipped_because.value if candidate.skipped_because else 'ok'}"
        + (f": {candidate.detail}" if candidate.detail else "")
        + ")"
        for candidate in candidates
    )


def resolve_runner(
    config: DispatchConfig,
    settings: ProjectDispatchSettings,
    *,
    group: Optional[str] = None,
) -> RunnerSelection:
    """Walk the precedence ladder and return the runner with its full account.

    Narrowest first, exactly the ladder design section 4 decided for profiles, with the
    profile rungs not yet built:

    1. a group named on **this dispatch**;
    2. *(unbuilt)* a profile named on this dispatch, mapping difficulty to a group;
    3. ``projects.<id>.group``;
    4. *(unbuilt)* a machine default profile;
    5. ``default_group:``, the machine-wide group;
    6. ``projects.<id>.runner`` -- today's behaviour, and the fallback.

    Rung 6 is reached only when no group applies at all, which is what makes an existing
    flat config behave exactly as it did. It is *not* reached when a group applies and
    turns out to be exhausted: see ``NoEligibleRunnerError``.
    """
    for name, source in (
        (group, SelectionSource.DISPATCH),
        (settings.group, SelectionSource.PROJECT),
        (config.default_group, SelectionSource.MACHINE),
    ):
        if not name:
            continue
        definition = config.runner_groups.get(name)
        if definition is None:
            raise UnknownGroupError(
                f"{_group_origin(source, settings.project_id)} names runner group "
                f"{name!r}, which is not defined in {config.path}. Known groups: "
                f"{_known(config.runner_groups)}. Groups are written by hand, on this "
                "machine, and never by a project or the browser."
            )
        return select_runner(definition, config.runners, source=source)

    if not settings.runner:
        raise UnknownRunnerError(
            f"Project {settings.project_id!r} is enabled for dispatch but names no "
            f"runner and no group. Set projects.{settings.project_id}.runner or "
            f".group in {config.path}."
        )

    runner = config.runners.get(settings.runner)
    if runner is None:
        raise UnknownRunnerError(
            f"Project {settings.project_id!r} names runner {settings.runner!r}, which "
            f"is not defined in {config.path}. Known runners: {_known(config.runners)}."
        )

    return RunnerSelection(runner=runner, source=SelectionSource.PROJECT_RUNNER)


def _group_origin(source: SelectionSource, project_id: str) -> str:
    """Where a group name came from, so an unknown one says which line to fix."""
    if source is SelectionSource.DISPATCH:
        return "This dispatch"
    if source is SelectionSource.PROJECT:
        return f"Project {project_id!r}"
    return "default_group"


# ----- the gates --------------------------------------------------------------


def assert_dispatch_permitted(
    project_id: str, home: Optional[Path] = None, *, group: Optional[str] = None
) -> DispatchResolution:
    """Walk every dispatch gate for ``project_id`` and resolve its runner.

    The single entry point the API, the CLI and the supervisor all call. Raises a
    ``DispatchError`` subclass naming the gate that refused; returns the resolved runner
    only when all four are open.

    ``group`` is the group named on this one dispatch, and it is the narrowest rung of
    the precedence ladder in ``resolve_runner``. It is an argument to *this* function
    rather than a separate step around it: group selection happens strictly after the
    four gates have all opened, so naming a group can never route past one. Passing it
    to a machine whose config has no groups is a refusal, not a silent fallback.

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

    # Gate 2, and only now: every other gate is about whether this machine will run
    # anything at all, and none of them may be reachable from a caller's choice of group.
    selection = resolve_runner(config, settings, group=group)

    return DispatchResolution(
        project_id=project_id,
        runner=selection.runner,
        settings=settings,
        limits=config.limits,
        config=config,
        selection=selection if selection.from_group else None,
    )


# ----- mutation ---------------------------------------------------------------


def set_project_enabled(
    project_id: str,
    enabled: bool,
    *,
    runner: Optional[str] = None,
    group: Optional[str] = None,
    home: Optional[Path] = None,
) -> ProjectDispatchSettings:
    """Turn dispatch on or off for one project, and return its resulting settings.

    Enablement is the only part of this file a browser or a CLI may write. Runners and
    groups are never created here: a project can only be pointed at a command, or a list
    of commands, that a human already wrote into this machine's config by hand, which is
    what keeps the reachable execution surface exactly as wide as that file says (design
    section 6, gate 3). Pointing at an existing group is the same act as pointing at an
    existing runner and is allowed on the same terms; authoring one is not.

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
        if runner is not None and group is not None:
            raise DispatchError(
                "Give a runner or a group, not both. A group already says which runners "
                "are in play, so naming one of them beside it says two things at once."
            )
        if group is not None:
            if group not in config.runner_groups:
                raise UnknownGroupError(
                    f"No runner group named {group!r} is defined in {path}. "
                    f"Known groups: {_known(config.runner_groups)}. Groups are written "
                    "by hand, on this machine, and never by a project or the browser."
                )
            entry["group"] = group
            entry.pop("runner", None)
        elif not _already_grouped(entry, config):
            entry["runner"] = _runner_for_enable(
                project_id, runner, entry.get("runner"), config, path
            )
    elif runner is not None or group is not None:
        raise DispatchError("--runner and --group are meaningless when disabling a project.")

    entry["enabled"] = enabled

    path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )

    updated = load_dispatch_config(home)
    assert updated is not None
    return updated.project(project_id)


def _already_grouped(entry: Mapping[str, object], config: DispatchConfig) -> bool:
    """True when this project already resolves through a group, so no runner is needed.

    A machine-wide ``default_group`` counts. Demanding a runner for a project that is
    about to resolve through a group would make ``dispatch enable`` the one place in the
    system that does not understand the precedence ladder the dispatcher uses.
    """
    existing = entry.get("group")
    if isinstance(existing, str) and existing:
        return True
    return bool(config.default_group)


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
