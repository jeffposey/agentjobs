"""Tests for the machine-level project registry.

The registry is the only place a project id becomes a filesystem path, so these tests
carry the containment guarantees for the whole multi-project feature.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentjobs.projects import (
    AmbiguousProjectError,
    ProjectError,
    ProjectRegistry,
    UnknownProjectError,
    contained_path,
    slugify_project_id,
)


@pytest.fixture()
def registry(tmp_path: Path) -> ProjectRegistry:
    """A registry rooted in a temp dir, never the real ~/.agentjobs."""
    return ProjectRegistry(home=tmp_path / "home")


def make_project(tmp_path: Path, name: str, tasks_dir: str = "tasks") -> Path:
    """Create a project directory with an AgentJobs config."""
    root = tmp_path / name
    (root / ".agentjobs").mkdir(parents=True)
    (root / tasks_dir).mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": name, "tasks_directory": tasks_dir}),
        encoding="utf-8",
    )
    return root


class TestRegistration:
    def test_add_and_get_roundtrip(self, registry: ProjectRegistry, tmp_path: Path) -> None:
        root = make_project(tmp_path, "alpha")
        added = registry.add(root)

        assert added.id == "alpha"
        assert added.root == root.resolve()
        assert registry.get("alpha").root == root.resolve()

    def test_id_derives_from_config_project_name(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        root = tmp_path / "some-checkout-dir"
        (root / ".agentjobs").mkdir(parents=True)
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": "My Project"}), encoding="utf-8"
        )

        assert registry.add(root).id == "my-project"

    def test_explicit_id_and_name_win(self, registry: ProjectRegistry, tmp_path: Path) -> None:
        root = make_project(tmp_path, "alpha")
        project = registry.add(root, project_id="a", name="Alpha Project")

        assert (project.id, project.name) == ("a", "Alpha Project")

    def test_registry_survives_a_reload(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        make_project(tmp_path, "alpha")
        ProjectRegistry(home=home).add(tmp_path / "alpha")

        assert [p.id for p in ProjectRegistry(home=home).list_projects()] == ["alpha"]

    def test_rejects_a_non_directory(self, registry: ProjectRegistry, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        with pytest.raises(ProjectError, match="Not a directory"):
            registry.add(missing)

    def test_rejects_reregistering_the_same_root_under_a_new_id(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        root = make_project(tmp_path, "alpha")
        registry.add(root)

        with pytest.raises(ProjectError, match="already registered"):
            registry.add(root, project_id="alpha-again")

    def test_readding_the_same_id_replaces_rather_than_duplicates(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        make_project(tmp_path, "alpha")
        second = make_project(tmp_path, "beta")
        registry.add(tmp_path / "alpha", project_id="shared")
        registry.add(second, project_id="shared")

        assert len(registry.list_projects()) == 1
        assert registry.get("shared").root == second.resolve()

    def test_remove(self, registry: ProjectRegistry, tmp_path: Path) -> None:
        root = make_project(tmp_path, "alpha")
        registry.add(root)
        registry.remove("alpha")

        assert registry.list_projects() == []
        assert root.exists(), "removing a registration must never touch project files"

    def test_remove_unknown_raises(self, registry: ProjectRegistry) -> None:
        with pytest.raises(UnknownProjectError):
            registry.remove("ghost")

    def test_projects_are_listed_in_id_order(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        for name in ("charlie", "alpha", "bravo"):
            registry.add(make_project(tmp_path, name))

        assert [p.id for p in registry.list_projects()] == ["alpha", "bravo", "charlie"]


class TestLookupSafety:
    """A project id must never be usable as a path component."""

    def test_unknown_id_names_what_is_registered(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        registry.add(make_project(tmp_path, "alpha"))

        with pytest.raises(UnknownProjectError, match="alpha"):
            registry.get("beta")

    @pytest.mark.parametrize(
        "hostile_id",
        ["..", "../alpha", "..\\alpha", "/etc", "C:/Windows", "alpha/../beta", ""],
    )
    def test_traversal_shaped_ids_are_unknown_not_resolved(
        self, registry: ProjectRegistry, tmp_path: Path, hostile_id: str
    ) -> None:
        registry.add(make_project(tmp_path, "alpha"))

        with pytest.raises(UnknownProjectError):
            registry.get(hostile_id)

    @pytest.mark.parametrize("hostile_id", ["..", "../escape", "a/b", "A-Upper", "-lead"])
    def test_registration_rejects_invalid_ids(
        self, registry: ProjectRegistry, tmp_path: Path, hostile_id: str
    ) -> None:
        root = make_project(tmp_path, "alpha")

        with pytest.raises(ProjectError):
            registry.add(root, project_id=hostile_id)

    def test_slugify_rejects_input_with_no_usable_characters(self) -> None:
        with pytest.raises(ProjectError):
            slugify_project_id("...")


class TestContainedPath:
    def test_allows_a_plain_filename(self, tmp_path: Path) -> None:
        assert contained_path(tmp_path, "task-001.yaml") == (tmp_path / "task-001.yaml").resolve()

    @pytest.mark.parametrize(
        "hostile",
        [
            "../escaped.yaml",
            "../../etc/passwd",
            "..\\..\\escaped.yaml",
            "sub/../../escaped.yaml",
        ],
    )
    def test_rejects_traversal(self, tmp_path: Path, hostile: str) -> None:
        with pytest.raises(ProjectError, match="outside the project directory"):
            contained_path(tmp_path, hostile)

    def test_rejects_an_absolute_path_that_would_discard_the_base(self, tmp_path: Path) -> None:
        # `base / "/etc/passwd"` silently discards `base`; so does a drive-qualified
        # path on Windows. Both must be refused rather than joined.
        outside = tmp_path.parent / "outside.yaml"
        with pytest.raises(ProjectError, match="outside the project directory"):
            contained_path(tmp_path, str(outside))


class TestDefaultResolution:
    def test_cwd_inside_a_project_selects_it(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        registry.add(make_project(tmp_path, "alpha"))
        registry.add(make_project(tmp_path, "beta"))

        resolved = registry.resolve_default(cwd=tmp_path / "beta" / "tasks")

        assert resolved.id == "beta"

    def test_a_sole_registered_project_is_the_default_from_anywhere(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        registry.add(make_project(tmp_path, "alpha"))

        assert registry.resolve_default(cwd=tmp_path.parent).id == "alpha"

    def test_nested_projects_resolve_to_the_deepest_root(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        outer = make_project(tmp_path, "outer")
        inner = make_project(outer, "inner")
        registry.add(outer)
        registry.add(inner)

        assert registry.resolve_default(cwd=inner).id == "inner"

    def test_ambiguity_raises_rather_than_guessing(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        registry.add(make_project(tmp_path, "alpha"))
        registry.add(make_project(tmp_path, "beta"))

        with pytest.raises(AmbiguousProjectError, match="alpha, beta"):
            registry.resolve_default(cwd=tmp_path.parent)

    def test_empty_registry_says_how_to_fix_it(self, registry: ProjectRegistry) -> None:
        with pytest.raises(AmbiguousProjectError, match="agentjobs project add"):
            registry.resolve_default()


class TestProjectConfig:
    def test_tasks_dir_comes_from_the_projects_own_config(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        root = make_project(tmp_path, "alpha", tasks_dir="work/items")
        project = registry.add(root)

        assert project.tasks_dir() == (root / "work" / "items").resolve()

    def test_tasks_dir_defaults_when_the_project_has_no_config(
        self, registry: ProjectRegistry, tmp_path: Path
    ) -> None:
        root = tmp_path / "bare"
        root.mkdir()
        project = registry.add(root)

        assert project.tasks_dir() == (root / "tasks").resolve()
        assert project.load_config() == {}
