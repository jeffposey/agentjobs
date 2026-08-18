"""Tests for dispatch configuration: every refusal path, and substitution safety.

The gates these exercise are the whole security model of dispatch, so the tests are
written as refusals first. The autouse ``isolate_project_registry`` fixture in
conftest.py already points ``AGENTJOBS_HOME`` at a temp directory, which is what keeps
these off a real ``~/.agentjobs``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentjobs.dispatch.config import (
    CONFIG_FILENAME,
    SENTINEL_FILENAME,
    DispatchConfigError,
    DispatchDisabledError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    PlaceholderError,
    Posture,
    ProjectNotEnabledError,
    RunnerMode,
    UnknownRunnerError,
    assert_dispatch_permitted,
    load_dispatch_config,
    set_project_enabled,
    substitute_argv,
)


def home() -> Path:
    """The temp AgentJobs home this test is isolated to."""
    return Path(os.environ["AGENTJOBS_HOME"])


def write_config(**overrides: object) -> Path:
    """Write a dispatch.yaml that permits 'agentjobs' unless overridden."""
    config: dict = {
        "version": 1,
        "enabled": True,
        "runners": {
            "claude": {"argv": ["claude", "-p", "{prompt}"], "env": {}},
            "codex": {"argv": ["codex", "exec", "{prompt}"], "mode": "session"},
        },
        "projects": {
            "agentjobs": {"enabled": True, "runner": "claude"},
        },
    }
    config.update(overrides)
    path = home() / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


class TestLoading:
    def test_absent_file_is_dispatch_off_not_an_error(self) -> None:
        assert load_dispatch_config() is None

        with pytest.raises(DispatchNotConfiguredError) as caught:
            assert_dispatch_permitted("agentjobs")
        assert caught.value.reason == "not_configured"

    def test_reads_the_home_the_environment_names(self) -> None:
        path = write_config()

        config = load_dispatch_config()

        assert config is not None
        assert config.path == path
        assert config.enabled is True
        assert set(config.runners) == {"claude", "codex"}

    def test_runner_mode_defaults_to_batch_and_is_declared_per_runner(self) -> None:
        write_config()

        config = load_dispatch_config()

        assert config is not None
        assert config.runners["claude"].mode is RunnerMode.BATCH
        assert config.runners["codex"].mode is RunnerMode.SESSION

    def test_posture_defaults_to_supervised(self) -> None:
        write_config()

        config = load_dispatch_config()

        assert config is not None
        assert config.project("agentjobs").posture is Posture.SUPERVISED

    def test_limits_default_to_the_designs_conservative_values(self) -> None:
        write_config()

        config = load_dispatch_config()

        assert config is not None
        assert config.limits.max_concurrent_runs == 1
        assert config.limits.run_timeout_seconds == 1800
        assert config.limits.session_stale_seconds == 3600
        assert config.limits.auto.per_task_per_day == 3
        assert config.limits.auto.per_task_lifetime == 10
        assert config.limits.auto.cooldown_seconds == 60

    def test_limits_are_read_from_the_file_when_present(self) -> None:
        write_config(limits={"max_concurrent_runs": 3, "auto": {"cooldown_seconds": 120}})

        config = load_dispatch_config()

        assert config is not None
        assert config.limits.max_concurrent_runs == 3
        assert config.limits.auto.cooldown_seconds == 120
        assert config.limits.run_timeout_seconds == 1800

    def test_config_is_reread_per_call_never_cached(self) -> None:
        write_config()
        first = load_dispatch_config()
        assert first is not None and first.enabled is True

        write_config(enabled=False)

        config = load_dispatch_config()
        assert config is not None
        assert config.enabled is False

    def test_unsupported_version_is_refused(self) -> None:
        write_config(version=2)

        with pytest.raises(DispatchConfigError):
            load_dispatch_config()

    def test_argv_must_be_a_non_empty_list_of_strings(self) -> None:
        write_config(runners={"claude": {"argv": "claude -p {prompt}"}})

        with pytest.raises(DispatchConfigError) as caught:
            load_dispatch_config()
        assert "argv" in str(caught.value)

    def test_a_typo_in_a_placeholder_is_a_config_error_not_a_literal_brace(self) -> None:
        write_config(runners={"claude": {"argv": ["claude", "-p", "{propmt}"]}})

        with pytest.raises(DispatchConfigError) as caught:
            load_dispatch_config()
        assert "propmt" in str(caught.value)

    def test_json_braces_in_argv_are_not_mistaken_for_placeholders(self) -> None:
        """The permission allow-list is passed as --settings JSON. It must survive."""
        settings = '{"permissions": {"allow": ["Bash(poetry run pytest:*)"]}}'
        write_config(runners={"claude": {"argv": ["claude", "--settings", settings]}})

        config = load_dispatch_config()

        assert config is not None
        assert config.runners["claude"].argv[2] == settings


class TestGates:
    def test_master_switch_off_refuses_even_an_enabled_project(self) -> None:
        write_config(enabled=False)

        with pytest.raises(DispatchDisabledError) as caught:
            assert_dispatch_permitted("agentjobs")
        assert caught.value.reason == "disabled"

    def test_project_not_listed_is_refused(self) -> None:
        write_config(projects={})

        with pytest.raises(ProjectNotEnabledError) as caught:
            assert_dispatch_permitted("agentjobs")
        assert caught.value.reason == "project_not_enabled"

    def test_project_listed_but_disabled_is_refused(self) -> None:
        write_config(projects={"agentjobs": {"enabled": False, "runner": "claude"}})

        with pytest.raises(ProjectNotEnabledError):
            assert_dispatch_permitted("agentjobs")

    def test_unknown_runner_is_its_own_error_not_a_broken_file(self) -> None:
        write_config(projects={"agentjobs": {"enabled": True, "runner": "ghost"}})

        with pytest.raises(UnknownRunnerError) as caught:
            assert_dispatch_permitted("agentjobs")
        assert caught.value.reason == "unknown_runner"
        assert "claude" in str(caught.value)

    def test_enabled_project_naming_no_runner_is_refused(self) -> None:
        write_config(projects={"agentjobs": {"enabled": True}})

        with pytest.raises(UnknownRunnerError):
            assert_dispatch_permitted("agentjobs")

    def test_every_gate_open_resolves_the_runner(self) -> None:
        write_config()

        resolution = assert_dispatch_permitted("agentjobs")

        assert resolution.runner.name == "claude"
        assert resolution.settings.enabled is True
        assert resolution.limits.max_concurrent_runs == 1

    def test_sentinel_refuses_when_everything_else_permits(self) -> None:
        write_config()
        assert assert_dispatch_permitted("agentjobs").runner.name == "claude"

        (home() / SENTINEL_FILENAME).write_text("", encoding="utf-8")

        with pytest.raises(DispatchSentinelError) as caught:
            assert_dispatch_permitted("agentjobs")
        assert caught.value.reason == "sentinel"
        assert SENTINEL_FILENAME in str(caught.value)

    def test_removing_the_sentinel_restores_dispatch(self) -> None:
        write_config()
        sentinel = home() / SENTINEL_FILENAME
        sentinel.write_text("", encoding="utf-8")
        sentinel.unlink()

        assert assert_dispatch_permitted("agentjobs").runner.name == "claude"


class TestSubstitution:
    def test_substitutes_every_documented_placeholder(self) -> None:
        argv = [
            "{agent}",
            "--task={task_id}",
            "--project={project_id}",
            "--root={project_root}",
            "--run={run_id}",
            "--api={api_base}",
            "{prompt}",
        ]

        rendered = substitute_argv(
            argv,
            {
                "agent": "claude",
                "task_id": "task-068",
                "project_id": "agentjobs",
                "project_root": "C:/projects/agentjobs",
                "run_id": "run_a1b2",
                "api_base": "http://127.0.0.1:8876",
                "prompt": "read the record",
            },
        )

        assert rendered == [
            "claude",
            "--task=task-068",
            "--project=agentjobs",
            "--root=C:/projects/agentjobs",
            "--run=run_a1b2",
            "--api=http://127.0.0.1:8876",
            "read the record",
        ]

    def test_a_hostile_prompt_stays_exactly_one_argv_element(self) -> None:
        """The reason argv is a list and there is no shell anywhere in dispatch."""
        prompt = (
            'read the record; rm -rf / && echo "pwned" `whoami` $(id) | tee /tmp/x\n'
            "second line with 'quotes' and {braces} and a trailing backslash \\"
        )

        rendered = substitute_argv(["claude", "-p", "{prompt}"], {"prompt": prompt})

        assert len(rendered) == 3
        assert rendered[2] == prompt
        assert rendered[2].count("\n") == 1

    def test_placeholders_inside_a_larger_element_are_substituted(self) -> None:
        rendered = substitute_argv(["--worktree=aj-{task_id}"], {"task_id": "068"})

        assert rendered == ["--worktree=aj-068"]

    def test_a_missing_value_is_a_typed_error(self) -> None:
        with pytest.raises(PlaceholderError):
            substitute_argv(["claude", "-p", "{prompt}"], {})

    def test_substitution_is_not_recursive(self) -> None:
        """A value that itself looks like a placeholder is not substituted again."""
        rendered = substitute_argv(["{prompt}"], {"prompt": "{task_id}", "task_id": "068"})

        assert rendered == ["{task_id}"]

    def test_runner_render_uses_the_same_substitution(self) -> None:
        write_config()
        resolution = assert_dispatch_permitted("agentjobs")

        assert resolution.runner.render({"prompt": "hi"}) == ["claude", "-p", "hi"]


class TestSetProjectEnabled:
    def test_refuses_when_there_is_no_config_at_all(self) -> None:
        with pytest.raises(DispatchNotConfiguredError):
            set_project_enabled("agentjobs", True)

    def test_refuses_a_runner_this_machine_does_not_define(self) -> None:
        write_config()

        with pytest.raises(UnknownRunnerError) as caught:
            set_project_enabled("agentjobs", True, runner="ghost")
        assert "codex" in str(caught.value)

    def test_keeps_unknown_keys_the_build_does_not_understand(self) -> None:
        path = write_config(future_setting={"kept": True})

        set_project_enabled("agentjobs", False)

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["future_setting"] == {"kept": True}
        assert raw["projects"]["agentjobs"]["enabled"] is False

    def test_enabling_a_new_project_picks_the_sole_runner(self) -> None:
        write_config(runners={"claude": {"argv": ["claude", "-p", "{prompt}"]}}, projects={})

        settings = set_project_enabled("newproj", True)

        assert settings.enabled is True
        assert settings.runner == "claude"

    def test_enabling_a_new_project_with_several_runners_needs_an_explicit_choice(
        self,
    ) -> None:
        write_config(projects={})

        with pytest.raises(UnknownRunnerError):
            set_project_enabled("newproj", True)
