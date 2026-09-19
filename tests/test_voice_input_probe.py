"""The task-171 dictation probe page holds together.

The probe is a measuring instrument, so a defect in it does not look like a defect --
it looks like a finding about a browser. These check the two ways it can lie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from starlette.testclient import TestClient

from scripts.voice_input_probe import (
    CRITICAL_TOKENS,
    REFERENCE_TEXT,
    build_page,
    make_app,
)


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
        assert (
            unescaped.count('"') % 2 == 0
        ), f"line {number} of the probe script leaves a string literal open: {line!r}"


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
    listener = re.search(r"plain\.addEventListener\(\"beforeinput\".*?\}\);", page, re.DOTALL)
    assert listener, "the probe no longer records what reached the plain field"
    assert "preventDefault" not in listener.group(0)


def test_a_device_can_actually_submit_its_findings(tmp_path: Path) -> None:
    """The one round trip that matters, exercised the way a phone exercises it.

    This is not a formality. The submission endpoint answered `422` to every device,
    because this module uses postponed annotations and FastAPI resolves them against
    module globals -- a function-local `Request` import left the parameter reclassified
    as a query parameter. Nothing in the page, the log or the process said so. The only
    place it was visible was here: a person finishing the work and being told "server
    said 422".
    """
    results = tmp_path / "results.jsonl"
    client = TestClient(make_app(results))

    assert client.get("/results").text.startswith("nothing reported yet")

    payload = {
        "label": "a device",
        "env": {"userAgent": "some browser", "secureContext": True},
        "features": {"SpeechRecognition": False},
        "runs": {},
        "plain": {"events": ["insertCompositionText"], "text": "dictated"},
    }
    response = client.post("/record", json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}

    written = [json.loads(line) for line in results.read_text().splitlines()]
    assert len(written) == 1
    assert written[0]["label"] == "a device"
    assert written[0]["plain"]["events"] == ["insertCompositionText"]
    assert "received" in written[0]
    assert "a device" in client.get("/results").text


def test_the_page_is_served_with_the_policy_it_measures(tmp_path: Path) -> None:
    """The on-device API is gated by a Permissions-Policy, so the probe states it.

    Relying on the same-origin default would make the policy one more variable in a
    measurement that exists to remove variables.
    """
    client = TestClient(make_app(tmp_path / "results.jsonl"))
    response = client.get("/")
    assert response.status_code == 200
    policy = response.headers["permissions-policy"]
    assert "microphone=(self)" in policy
    assert "on-device-speech-recognition=(self)" in policy
