"""Tests for splicing an edit into hand-written YAML (task-198).

The dispatch config tests prove the writer keeps a realistic file intact. These pin the
shapes that realistic file does not happen to contain, and -- as important -- the shapes
the patch must refuse rather than guess at.
"""

from __future__ import annotations

import copy
from typing import Sequence

import pytest
import yaml

from agentjobs.dispatch.yaml_patch import Delete, Edit, Set, UnpatchableYaml, patch_yaml


def apply(text: str, *edits: Edit) -> str:
    """Patch ``text``, expecting exactly what the same edits do to the parsed mapping."""
    return patch_yaml(text, edits, expected(text, edits))


def expected(text: str, edits: Sequence[Edit]) -> dict:
    data = copy.deepcopy(yaml.safe_load(text) or {})
    for edit in edits:
        cursor = data
        for key in edit.path[:-1]:
            if not isinstance(cursor.get(key), dict):
                cursor[key] = {}
            cursor = cursor[key]
        if isinstance(edit, Set):
            cursor[edit.path[-1]] = edit.value
        else:
            cursor.pop(edit.path[-1], None)
    return data


class TestSplicing:
    def test_a_value_is_replaced_in_place_keeping_its_trailing_comment(self) -> None:
        text = "projects:\n  p:\n    enabled: false   # why it is off\n"

        assert apply(text, Set(("projects", "p", "enabled"), True)) == (
            "projects:\n  p:\n    enabled: true   # why it is off\n"
        )

    def test_a_value_already_equal_keeps_its_quoting(self) -> None:
        text = "projects:\n  p:\n    runner: 'claude'\n"

        assert apply(text, Set(("projects", "p", "runner"), "claude")) == text

    def test_a_value_that_only_looks_equal_is_rewritten(self) -> None:
        text = "projects:\n  p:\n    enabled: 'true'\n"

        assert apply(text, Set(("projects", "p", "enabled"), True)) == (
            "projects:\n  p:\n    enabled: true\n"
        )

    def test_an_empty_value_is_filled_before_its_comment(self) -> None:
        text = "projects:\n  p:\n    enabled:  # unset\n"

        assert apply(text, Set(("projects", "p", "enabled"), False)) == (
            "projects:\n  p:\n    enabled: false  # unset\n"
        )

    def test_an_inline_empty_mapping_becomes_a_block(self) -> None:
        text = "version: 1\nprojects: {}   # none yet\nlimits: {}\n"

        assert apply(text, Set(("projects", "q", "enabled"), True)) == (
            "version: 1\nprojects:   # none yet\n  q:\n    enabled: true\nlimits: {}\n"
        )

    def test_a_bare_key_grows_a_block(self) -> None:
        assert apply("version: 1\nprojects:\n", Set(("projects", "q", "enabled"), True)) == (
            "version: 1\nprojects:\n  q:\n    enabled: true\n"
        )

    def test_a_missing_top_level_block_is_appended(self) -> None:
        assert apply("version: 1", Set(("idle_sessions", "enforce"), True)) == (
            "version: 1\nidle_sessions:\n  enforce: true"
        )

    def test_a_new_key_follows_a_block_scalar_rather_than_landing_inside_it(self) -> None:
        text = "projects:\n  p:\n    note: |\n      two\n      lines\n# after\n"

        patched = apply(text, Set(("projects", "p", "enabled"), True))

        assert yaml.safe_load(patched)["projects"]["p"] == {"note": "two\nlines\n", "enabled": True}
        assert patched.endswith("    enabled: true\n# after\n")

    def test_the_files_own_indentation_width_is_used(self) -> None:
        text = "projects:\n    p:\n        enabled: true\n"

        assert apply(text, Set(("projects", "q", "enabled"), False)) == (
            text + "    q:\n        enabled: false\n"
        )

    def test_a_deleted_key_takes_only_its_own_line(self) -> None:
        text = "projects:\n  p:\n    # the runner\n    runner: a  # gone\n    enabled: true\n"

        assert apply(text, Delete(("projects", "p", "runner"))) == (
            "projects:\n  p:\n    # the runner\n    enabled: true\n"
        )

    def test_deleting_a_missing_key_changes_nothing(self) -> None:
        text = "projects:\n  p:\n    enabled: true\n"

        assert apply(text, Delete(("projects", "p", "runner"))) == text

    def test_crlf_is_used_for_inserted_lines(self) -> None:
        text = "projects:\r\n  p:\r\n    enabled: true\r\n"

        assert apply(text, Set(("projects", "q", "enabled"), True)) == (
            text + "  q:\r\n    enabled: true\r\n"
        )


class TestRefusals:
    def test_growing_a_flow_mapping_is_refused(self) -> None:
        with pytest.raises(UnpatchableYaml):
            apply("projects:\n  p: {runner: a}\n", Set(("projects", "p", "enabled"), True))

    def test_replacing_a_collection_with_a_scalar_is_refused(self) -> None:
        with pytest.raises(UnpatchableYaml):
            apply("projects:\n  p:\n    runner: [a]\n", Set(("projects", "p", "runner"), "a"))

    def test_emptying_a_mapping_is_refused(self) -> None:
        with pytest.raises(UnpatchableYaml):
            apply("projects:\n  p:\n    runner: a\n", Delete(("projects", "p", "runner")))

    def test_a_result_that_parses_differently_is_refused(self) -> None:
        text = "projects:\n  p:\n    enabled: true\n"

        with pytest.raises(UnpatchableYaml, match="other than intended"):
            patch_yaml(text, [Set(("projects", "p", "enabled"), False)], {"projects": {}})
