"""The task-171 dictation probe page holds together.

The probe is a measuring instrument, so a defect in it does not look like a defect --
it looks like a finding about a browser. These check the two ways it can lie.
"""

from __future__ import annotations

import json
import re

from scripts.voice_input_probe import CRITICAL_TOKENS, REFERENCE_TEXT, build_page


def _script(page: str) -> str:
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match, "the probe page has no script block"
    return match.group(1)


def test_every_js_string_literal_closes_on_its_own_line() -> None:
    """A page whose script does not parse still renders, and measures nothing.

    Writing the template without a raw prefix turned an escaped newline inside a JS
    string into a real one. The page looked correct, the buttons did nothing, and the
    browser reported it as a single console line nobody was reading. Every string in
    this script is single-line, so an odd number of unescaped quotes on a line is that
    failure and nothing else.
    """
    script = _script(build_page())
    # Comments go first: they are prose, and this script's prose contains both
    # backticks and apostrophes. The script has no URLs, so splitting on `//` cannot
    # eat a scheme.
    assert "://" not in script, "a URL would break the comment stripping below"
    code = [line.split("//", 1)[0] for line in script.splitlines()]
    assert "`" not in "".join(code), (
        "a template literal may legitimately span lines, which would make the "
        "quote-balance check below meaningless -- change the check, not this assert"
    )
    for number, line in enumerate(code, start=1):
        unescaped = re.sub(r"\\.", "", line)
        assert unescaped.count('"') % 2 == 0, (
            f"line {number} of the probe script leaves a string literal open: {line!r}"
        )


def test_the_reference_prose_reaches_the_page_as_data() -> None:
    """Scoring a transcript means nothing unless the device read the same words."""
    page = build_page()
    assert json.dumps(REFERENCE_TEXT) in page
    assert json.dumps(CRITICAL_TOKENS) in page
    assert "%REFERENCE%" not in page and "%CRITICAL%" not in page


def test_the_plain_field_is_a_plain_field() -> None:
    """Path 1 of task-171 is the operating system keyboard's own microphone key.

    It needs no code and cannot be improved, only broken -- by a control that replaces
    the textarea or intercepts what arrives in it. The probe's fourth section exists to
    show that path still works, so it has to stay an ordinary field with nothing bound
    to its input path.
    """
    page = build_page()
    field = re.search(r"<textarea id=\"plain\"[^>]*>", page)
    assert field, "the probe lost its plain textarea"
    for handler in ("oninput", "onkeydown", "onkeypress", "onbeforeinput", "onchange"):
        assert handler not in field.group(0)
    # `beforeinput` is observed, never cancelled: a listener that called
    # preventDefault() would be the very breakage this section is here to detect.
    listener = re.search(
        r"plain\.addEventListener\(\"beforeinput\".*?\}\);", page, re.DOTALL
    )
    assert listener, "the probe no longer records what reached the plain field"
    assert "preventDefault" not in listener.group(0)
