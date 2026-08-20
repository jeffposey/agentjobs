"""Tests for dispatch configuration: every refusal path, and substitution safety.

The gates these exercise are the whole security model of dispatch, so the tests are
written as refusals first. The autouse ``isolate_project_registry`` fixture in
conftest.py already points ``AGENTJOBS_HOME`` at a temp directory, which is what keeps
these off a real ``~/.agentjobs``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

from agentjobs.dispatch.config import (
    CONFIG_FILENAME,
    SENTINEL_FILENAME,
    DispatchConfigError,
    DispatchDisabledError,
    DispatchError,
    DispatchNotConfiguredError,
    DispatchSentinelError,
    NoEligibleRunnerError,
    PlaceholderError,
    Posture,
    ProjectNotEnabledError,
    RunnerMode,
    SelectionSource,
    SkipReason,
    UnknownGroupError,
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

    def test_posture_defaults_to_auto(self) -> None:
        """Changed from supervised on 2026-08-19; see task-020.

        A project that names no posture gets the one that can actually finish work
        unattended. Supervised parks on the first command outside its allow-list.
        """
        write_config()

        config = load_dispatch_config()

        assert config is not None
        assert config.project("agentjobs").posture is Posture.AUTO

    def test_an_explicitly_named_posture_still_wins_over_the_default(self) -> None:
        """Changing the default must not quietly re-point projects that chose one."""
        write_config(
            projects={
                "agentjobs": {"enabled": True, "runner": "claude", "posture": "supervised"},
            }
        )

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
        rendered = substitute_argv(["--session-label=aj-{task_id}"], {"task_id": "068"})

        assert rendered == ["--session-label=aj-068"]

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


# ----- runner groups (task-177) -----------------------------------------------


HERE = sys.executable
"""An argv[0] that is certain to resolve on PATH, on every machine this runs on."""

MISSING = "agentjobs-no-such-program-xyz"
"""An argv[0] that is certain not to."""


def write_grouped_config(**overrides: object) -> Path:
    """A dispatch.yaml whose 'agentjobs' project resolves through a group."""
    config: dict = {
        "version": 1,
        "enabled": True,
        "runners": {
            "big": {"argv": [MISSING, "-p", "{prompt}"]},
            "small": {"argv": [HERE, "-c", "pass", "{prompt}"]},
            "spare": {"argv": [HERE, "-c", "pass", "{prompt}"]},
        },
        "runner_groups": {
            "default": {
                "description": "What a dispatch gets when it names nothing.",
                "members": [
                    {"runner": "big"},
                    {"runner": "small"},
                    {"runner": "spare"},
                ],
            }
        },
        "projects": {"agentjobs": {"enabled": True, "group": "default"}},
    }
    config.update(overrides)
    path = home() / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


class TestGroupParsing:
    """The shape of a group, and what the loader refuses."""

    def test_members_may_be_bare_strings(self) -> None:
        write_grouped_config(runner_groups={"default": {"members": ["small", "spare"]}})
        config = load_dispatch_config(home())
        assert config is not None
        group = config.runner_groups["default"]
        assert [member.runner for member in group.members] == ["small", "spare"]
        assert all(member.enabled for member in group.members)

    def test_a_member_can_be_disabled_with_a_note(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {
                    "members": [
                        "small",
                        {"runner": "big", "enabled": False, "note": "no key yet"},
                    ]
                }
            },
        )
        config = load_dispatch_config(home())
        assert config is not None
        second = config.runner_groups["default"].members[1]
        assert second.enabled is False
        assert second.note == "no key yet"

    def test_a_member_may_name_a_runner_that_does_not_exist_yet(self) -> None:
        """Deferred to selection, exactly like a project naming an unknown runner.

        Writing the second option before configuring it is the state ``enabled: false``
        exists to represent; refusing to load the file would make it unrepresentable.
        """
        write_grouped_config(runner_groups={"default": {"members": ["small", "later"]}})
        assert load_dispatch_config(home()) is not None

    def test_an_empty_group_is_refused(self) -> None:
        write_grouped_config(runner_groups={"default": {"members": []}})
        with pytest.raises(DispatchConfigError, match="non-empty"):
            load_dispatch_config(home())

    def test_a_duplicate_member_is_refused(self) -> None:
        write_grouped_config(runner_groups={"default": {"members": ["small", "small"]}})
        with pytest.raises(DispatchConfigError, match="twice"):
            load_dispatch_config(home())

    def test_default_group_must_be_a_string(self) -> None:
        write_grouped_config(default_group=["default"])
        with pytest.raises(DispatchConfigError, match="default_group"):
            load_dispatch_config(home())


class TestGroupSelection:
    """Which member runs, and what is recorded about the ones that did not."""

    def test_the_first_member_that_can_run_wins(self) -> None:
        write_grouped_config()
        resolution = assert_dispatch_permitted("agentjobs", home())
        assert resolution.runner.name == "small"

    def test_selection_is_deterministic(self) -> None:
        write_grouped_config()
        chosen = {assert_dispatch_permitted("agentjobs", home()).runner.name for _ in range(5)}
        assert chosen == {"small"}

    def test_every_candidate_is_accounted_for(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {
                    "members": [
                        {"runner": "gone", "enabled": True},
                        {"runner": "big"},
                        {"runner": "spare", "enabled": False, "note": "kept in reserve"},
                        {"runner": "small"},
                    ]
                }
            },
        )
        selection = assert_dispatch_permitted("agentjobs", home()).selection
        assert selection is not None
        assert selection.runner.name == "small"
        assert selection.group == "default"
        by_name = {candidate.runner: candidate for candidate in selection.candidates}
        assert by_name["gone"].skipped_because is SkipReason.UNDEFINED_RUNNER
        assert by_name["big"].skipped_because is SkipReason.EXECUTABLE_NOT_FOUND
        assert by_name["spare"].skipped_because is SkipReason.DISABLED
        assert by_name["spare"].detail == "kept in reserve"
        assert by_name["small"].eligible is True

    def test_a_disabled_member_is_never_selected(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {"members": [{"runner": "small", "enabled": False}, "spare"]}
            },
        )
        resolution = assert_dispatch_permitted("agentjobs", home())
        assert resolution.runner.name == "spare"

    def test_members_after_the_winner_are_listed_but_not_judged(self) -> None:
        """'Not reached' and 'rejected' are different facts and stay different."""
        write_grouped_config()
        selection = assert_dispatch_permitted("agentjobs", home()).selection
        assert selection is not None
        trailing = selection.candidates[-1]
        assert trailing.runner == "spare"
        assert trailing.eligible is True
        assert trailing.skipped_because is None

    def test_an_exhausted_group_refuses_rather_than_falling_back(self) -> None:
        write_grouped_config(
            projects={"agentjobs": {"enabled": True, "group": "default", "runner": "small"}},
            runner_groups={"default": {"members": [{"runner": "small", "enabled": False}, "big"]}},
        )
        with pytest.raises(NoEligibleRunnerError) as caught:
            assert_dispatch_permitted("agentjobs", home())
        assert "disabled" in str(caught.value)
        assert "executable_not_found" in str(caught.value)

    def test_an_unknown_group_is_a_refusal_naming_the_known_ones(self) -> None:
        write_grouped_config(projects={"agentjobs": {"enabled": True, "group": "nope"}})
        with pytest.raises(UnknownGroupError, match="Known groups: default"):
            assert_dispatch_permitted("agentjobs", home())


class TestGroupPrecedence:
    """The ladder, narrowest first (design section 4)."""

    def test_a_group_named_on_the_dispatch_beats_the_project(self) -> None:
        write_grouped_config(
            runner_groups={
                "default": {"members": ["small"]},
                "audit": {"members": ["spare"]},
            },
        )
        resolution = assert_dispatch_permitted("agentjobs", home(), group="audit")
        assert resolution.runner.name == "spare"
        assert resolution.selection is not None
        assert resolution.selection.source is SelectionSource.DISPATCH

    def test_the_project_group_beats_the_machine_default(self) -> None:
        write_grouped_config(
            default_group="audit",
            runner_groups={
                "default": {"members": ["small"]},
                "audit": {"members": ["spare"]},
            },
        )
        resolution = assert_dispatch_permitted("agentjobs", home())
        assert resolution.runner.name == "small"
        assert resolution.selection is not None
        assert resolution.selection.source is SelectionSource.PROJECT

    def test_the_machine_default_applies_when_the_project_names_no_group(self) -> None:
        write_grouped_config(
            default_group="audit",
            projects={"agentjobs": {"enabled": True, "runner": "small"}},
            runner_groups={"audit": {"members": ["spare"]}},
        )
        resolution = assert_dispatch_permitted("agentjobs", home())
        assert resolution.runner.name == "spare"
        assert resolution.selection is not None
        assert resolution.selection.source is SelectionSource.MACHINE

    def test_naming_a_group_on_a_machine_with_none_refuses(self) -> None:
        write_config()
        with pytest.raises(UnknownGroupError, match="none defined"):
            assert_dispatch_permitted("agentjobs", home(), group="audit")


class TestFlatConfigIsUntouched:
    """Backwards compatibility, stated as tests rather than as an intention."""

    def test_a_flat_config_still_resolves_its_project_runner(self) -> None:
        write_config()
        resolution = assert_dispatch_permitted("agentjobs", home())
        assert resolution.runner.name == "claude"

    def test_a_flat_config_records_no_selection_at_all(self) -> None:
        """The flag that keeps a flat setup's dispatch entry byte-identical."""
        write_config()
        assert assert_dispatch_permitted("agentjobs", home()).selection is None

    def test_a_flat_config_exposes_no_groups(self) -> None:
        write_config()
        config = load_dispatch_config(home())
        assert config is not None
        assert config.runner_groups == {}
        assert config.default_group is None

    def test_a_project_with_neither_runner_nor_group_still_refuses(self) -> None:
        write_config(projects={"agentjobs": {"enabled": True}})
        with pytest.raises(UnknownRunnerError, match="names no runner and no group"):
            assert_dispatch_permitted("agentjobs", home())


class TestEnablingAgainstAGroup:
    """``dispatch enable`` understands the same ladder the dispatcher does."""

    def test_a_project_can_be_pointed_at_an_existing_group(self) -> None:
        write_grouped_config(projects={})
        settings = set_project_enabled("agentjobs", True, group="default", home=home())
        assert settings.enabled is True
        assert settings.group == "default"
        assert settings.runner is None

    def test_pointing_at_an_undefined_group_is_refused(self) -> None:
        write_grouped_config(projects={})
        with pytest.raises(UnknownGroupError, match="written by hand"):
            set_project_enabled("agentjobs", True, group="invented", home=home())

    def test_a_runner_and_a_group_together_are_refused(self) -> None:
        write_grouped_config(projects={})
        with pytest.raises(DispatchError, match="not both"):
            set_project_enabled("agentjobs", True, runner="small", group="default", home=home())

    def test_enabling_an_already_grouped_project_needs_no_runner(self) -> None:
        write_grouped_config(projects={"agentjobs": {"enabled": False, "group": "default"}})
        settings = set_project_enabled("agentjobs", True, home=home())
        assert settings.enabled is True
        assert settings.group == "default"
