"""The committed LinkML output must match what the source would generate today.

`schema/agentjobs-v1.yaml` and `schema/agentjobs-v2.yaml` are the source of truth for
schema *shape*; everything under `schema/generated/` and `docs/schema/v1|v2/` is derived
from them by `scripts/regen-schema-docs.sh`. Nothing ran that script as part of any
check, so the generated artifacts were current only by whoever last remembered -- and
"current by memory" is the failure mode the Big Dawg Audit was called to find, not a
standard to keep.

This covers the cheapest and most load-bearing artifact: the JSON Schema. It is the one
an external consumer would validate against, and generating it costs about six seconds
against no network. The reference pages under `docs/schema/` are not covered here --
`gen-doc` writes ~160 files and takes long enough that putting it in the suite would be
a worse trade than the drift it catches. Run the script when you touch a schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "agentjobs-v1": ROOT / "schema" / "agentjobs-v1.yaml",
    "agentjobs-v2": ROOT / "schema" / "agentjobs-v2.yaml",
}
GENERATED = ROOT / "schema" / "generated"


def _generator() -> list[str] | None:
    """The `gen-json-schema` entry point, however this environment exposes it.

    Preferring the module form over the console script keeps this working in an
    environment whose `Scripts/`/`bin/` directory is not on `PATH` -- which is the
    normal state of a worktree's virtualenv when the gate invokes pytest directly by
    interpreter path.
    """
    try:
        import linkml.generators.jsonschemagen  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "linkml.generators.jsonschemagen"]


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_committed_json_schema_matches_source(name: str) -> None:
    generator = _generator()
    if generator is None:  # pragma: no cover - only on an install without linkml
        pytest.skip("linkml is not installed in this environment")

    source = SOURCES[name]
    committed = GENERATED / f"{name}.schema.json"
    assert source.is_file(), f"missing schema source: {source}"
    assert committed.is_file(), f"missing generated file: {committed}"

    result = subprocess.run(
        [*generator, str(source)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"gen-json-schema failed:\n{result.stderr}"

    # Compared as parsed JSON rather than as text: key order and trailing-newline
    # differences between generator versions are not schema drift, and failing on them
    # would train people to regenerate without reading the diff.
    regenerated = json.loads(result.stdout)
    on_disk = json.loads(committed.read_text(encoding="utf-8"))

    assert regenerated == on_disk, (
        f"{committed.relative_to(ROOT)} is out of date with "
        f"{source.relative_to(ROOT)}.\n"
        "Regenerate with: bash scripts/regen-schema-docs.sh"
    )


def test_the_regen_script_still_writes_what_this_test_checks() -> None:
    """Guard the coupling above, so a renamed output silently skips nothing.

    If someone changes where `regen-schema-docs.sh` puts the JSON Schema, the test
    above keeps passing against a file nothing regenerates any more. Reading the script
    for the path it writes is cheap insurance against that.
    """
    script = ROOT / "scripts" / "regen-schema-docs.sh"
    assert script.is_file(), "scripts/regen-schema-docs.sh is missing"
    text = script.read_text(encoding="utf-8")
    for name in SOURCES:
        assert f"{name}.schema.json" in text, (
            f"regen-schema-docs.sh no longer writes {name}.schema.json; "
            "update tests/test_schema_generated_is_current.py to match."
        )


def test_empty_generated_artifacts_are_not_mistaken_for_output() -> None:
    """Two zero-byte `.dbml` files sit in `schema/generated/` and nothing writes them.

    Found by the Big Dawg Audit. They are not produced by the regen script, so a reader
    who finds them reasonably assumes DBML generation exists and is broken. This test
    fails if either grows content -- at which point it is real output and belongs in the
    script -- and otherwise documents them as inert.
    """
    for stale in sorted(GENERATED.glob("*.dbml")):
        assert stale.stat().st_size == 0, (
            f"{stale.relative_to(ROOT)} now has content, but "
            "scripts/regen-schema-docs.sh does not generate it. Either add it to the "
            "script or delete the file."
        )
