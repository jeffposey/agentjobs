"""The always-loaded bundle has a ceiling, and this is it.

Every session in this repository loads ``CLAUDE.md`` and the three files it imports
before its first thought. Task-301 measured that cost: **24,902 of a dispatched
session's 75,619 opening tokens -- 32.9%**, the largest block anyone working here can
act on and 6.4x the entire MCP surface. It also measured the growth: between
2026-08-21T15:47 and 2026-08-25T09:57 the two large files gained 29,355 bytes, about
1,240 words a day.

Those two numbers together are why this test exists rather than a one-off cut. **A cut
without a ceiling is seasonal.** Task-305 removed 19,498 bytes and this asserts that
they stay removed.

It copies the pattern already used on the MCP leading rule in
``src/agentjobs/mcp/instructions.py`` -- a constant, the reasoning beside it, and a
cheap test -- because a budget nothing enforces is a statement of intent, which is the
same argument ``ENGINEERING.md`` makes for putting checks in the gate rather than in a
list.

**Bytes rather than words or tokens**, task-301's choice: ``len(text.encode())`` needs
no tokenizer, runs in a test, and means the same thing on every machine and both
runners. The conversions are recorded in ``docs/context-budget.md`` so a word-count
proposal can still be priced -- 3.0 bytes/token for Claude, 4.4 for Codex, 2.11
tokens/word.

Newlines are normalised because ``read_bundle`` reads text: a CRLF checkout on Windows
and an LF checkout elsewhere measure the same, which they would not if this counted the
files on disk.
"""

from __future__ import annotations

from pathlib import Path

from agentjobs.contexteval.bundle import BUNDLE_FILES, read_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent

BUNDLE_BUDGET_BYTES = 60_000
"""What the four always-loaded files may weigh together.

**This is a waypoint and not the destination.** Task-301 proposed 52,000 -- roughly where
the bundle stood on 2026-08-21, a day on which several sessions worked correctly against
it and nobody called it thin. Task-305 reached 58,557 and stopped there deliberately,
because closing the last 6,500 bytes would have meant removing rules rather than the
reasons for them, and its own spec says a budget that forces a rule out is a worse
outcome than a large file.

The arithmetic behind that judgement, so the next session can re-take it rather than
inherit it:

- The audit's target was **3,900 words** out of the two large files. Task-305 removed
  **3,358** -- and the files are now *185 words larger* than they were on 2026-08-21,
  because six days of genuinely new rules (task-293, task-303, task-308, the finish
  configuration, the queue notices) arrived in between and were kept.
- So the word target was substantially met. The byte anchor was not, because the anchor
  was a snapshot of a file that has since grown by rules rather than by narrative.

Lowering this number is the right instinct; do it by finding more rationale, incident
narrative or measurement to move, not by compressing a rule until it stops being one.
Raising it should take a commit message that says which rule needed the room.
"""

PER_FILE_GUIDANCE = {
    "ENGINEERING.md": 26_000,
    "ALLAGENTS.md": 23_000,
    "AGENTS.md": 2_000,
    "CLAUDE.md": 200,
}
"""Task-301's per-file proposal, deliberately **not** asserted.

Its report says to enforce the total and treat these as guidance for where to look, and
that is right: the four files trade content between themselves as material moves to the
place that owns it, and a per-file assertion would turn a correct move into a failure.
They are here so a failure message can say which file to look at.

``CLAUDE.md`` will not reach 200: about 700 of its bytes are an HTML comment that Claude
Code strips before the file enters context, so it is nearer 60 in the units that matter.
The budget counts it anyway, which only makes the ceiling stricter.
"""


def bundle_sizes() -> dict[str, int]:
    """Bytes per always-loaded file, newline-normalised."""
    bundle = read_bundle(REPO_ROOT)
    return {name: len(text.encode("utf-8")) for name, text in bundle.items()}


class TestBundleBudget:
    def test_every_bundle_file_is_present(self) -> None:
        """A missing file would make the budget pass by disappearing."""
        assert set(bundle_sizes()) == set(BUNDLE_FILES)

    def test_the_bundle_is_under_budget(self) -> None:
        sizes = bundle_sizes()
        total = sum(sizes.values())

        over = total - BUNDLE_BUDGET_BYTES
        detail = "\n".join(
            f"  {name:16} {sizes[name]:6}  (guidance {PER_FILE_GUIDANCE.get(name, 0)},"
            f" {sizes[name] - PER_FILE_GUIDANCE.get(name, 0):+})"
            for name in BUNDLE_FILES
        )
        assert total <= BUNDLE_BUDGET_BYTES, (
            f"the always-loaded bundle is {total} bytes, {over} over the "
            f"{BUNDLE_BUDGET_BYTES} budget:\n{detail}\n"
            "Every session pays this before its first thought. Move rationale, incident "
            "narrative or measurement history to docs/ or to the task record it came "
            "from -- see docs/context-budget.md. Raising BUNDLE_BUDGET_BYTES is a "
            "decision, not a fix: say in the commit which rule needed the room."
        )
