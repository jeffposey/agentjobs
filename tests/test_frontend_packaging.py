"""Release packaging contracts for the built React application."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a repository script without making scripts/ an installed package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_frontend = load_script("build_frontend")
build_release = load_script("build_release")
REQUIRED_FILES = build_frontend.REQUIRED_FILES
validate_bundle = build_frontend.validate_bundle
WHEEL_PREFIX = build_release.WHEEL_PREFIX
verify_wheel = build_release.verify_wheel


def write_complete_bundle(bundle: Path) -> None:
    """Create the minimum shape accepted by both release guards."""
    for relative in REQUIRED_FILES:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content")
    assets = bundle / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "index-hash.js").write_bytes(b"javascript")
    (assets / "index-hash.css").write_bytes(b"css")


def test_frontend_bundle_is_configured_as_sdist_and_wheel_package_data() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    includes = data["tool"]["poetry"]["include"]
    assert {
        "path": "src/agentjobs/frontend_dist",
        "format": ["sdist", "wheel"],
    } in includes
    assert "build" not in data["tool"]["poetry"]


def test_bundle_validation_names_missing_release_assets(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="manifest.webmanifest"):
        validate_bundle(tmp_path)


def test_bundle_validation_accepts_shell_assets_and_every_pwa_file(tmp_path: Path) -> None:
    write_complete_bundle(tmp_path)

    validate_bundle(tmp_path)


def test_finished_wheel_is_checked_for_frontend_and_pwa_members(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_complete_bundle(bundle)
    wheel = tmp_path / "agentjobs-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in bundle.rglob("*"):
            if path.is_file():
                archive.write(path, f"{WHEEL_PREFIX}{path.relative_to(bundle).as_posix()}")
        archive.writestr(
            "agentjobs-0.1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )

    wheel_bytes, bundle_bytes = verify_wheel(wheel)

    assert wheel_bytes == wheel.stat().st_size
    assert bundle_bytes > 0


def test_platform_specific_wheel_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "agentjobs-0.1.0-cp313-cp313-win_amd64.whl"

    with pytest.raises(RuntimeError, match="platform-independent"):
        verify_wheel(wheel)
