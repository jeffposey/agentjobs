"""Find where a task record quotes a person verbatim instead of paraphrasing them.

This repository has a public remote, and its task records are written by agents
summarising what a person said. The habit of quoting somebody verbatim to make a point
puts an attributed, informally worded remark into a public record, where it reads as a
characterisation of a real person rather than as engineering. Task-376 made that a
content rule; this module is the mechanical half of it.

**The rule the detector serves**

1.  A record states what a person *meant*, not the words they used. Direct quotation is
    for cases where the exact wording is the subject: an API name, a spec sentence being
    disputed, a message being debugged.
2.  Tone is never reproduced. Where a remark carried frustration, profanity or
    informality, the record summarises it professionally and keeps the substance.

**What the detector actually looks for, and what it cannot see**

It fires on the intersection of three signals, never on one alone:

*   a **quotation shape** -- a double-quoted run, a typographic-quoted run, or a
    Markdown block quote;
*   a **tone marker** inside that quotation, from one of the four groups in
    :data:`TONE_GROUPS`;
*   for every group but :data:`VULGARITY`, an **attribution cue** in the text just
    before the quotation, or a name that appears in this record's own log.

That is a deliberately narrow target, and the limits are worth stating plainly rather
than discovering later:

*   **It cannot see tone that uses no listed word.** A remark quoted verbatim in calm,
    plain language passes, and so does a sarcastic one built entirely out of ordinary
    words. The lexicons below are a floor, not a definition.
*   **It cannot see a paraphrase that reproduces tone.** Rule 2 above binds prose that
    is not in quotation marks at all, and nothing here checks that.
*   **It cannot tell a person's words from a machine's.** A quoted error message or log
    line that happens to contain a listed word, sitting after the word "said", is a
    false positive. The two-signal design makes these rare rather than impossible.
*   **It does not filter profanity generally.** Vulgarity outside a quotation is out of
    scope; the subject is how a record represents a person, not the register of its own
    prose.

The lexicons are ordinary dictionary words chosen for the categories they name. They
were not derived from anything this project has redacted, and no removed wording is
reproduced here or in the tests.

**Where it runs.** Three tiers, escalating with how durable the damage would be:
:mod:`agentjobs.record_check` warns the author at the moment of the write,
``tests/test_task_corpus.py`` fails the gate over ``tasks/``, and
:mod:`agentjobs.sqlstore.importer` refuses an import outright. The fix at any tier is
``agentjobs redact``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models_v2 import Task

__all__ = [
    "ATTRIBUTION_CUES",
    "DISMISSAL",
    "EMPHASIS",
    "INFORMALITY",
    "LOG_BODY_FIELD",
    "QuotedRemark",
    "SPEC_PROSE_FIELDS",
    "TASK_PROSE_FIELDS",
    "TONE_GROUPS",
    "VULGARITY",
    "field_text",
    "scan_task",
    "scan_text",
    "speakers_in",
]

#: Profanity and vulgarity. The one group that does not need an attribution cue: a
#: quoted run carrying it is a reproduced register whoever said it, and this
#: repository's technical prose does not contain these words inside quotation marks.
VULGARITY = "vulgarity"

#: Dismissive or derogatory judgement. Ordinary words that engineering prose uses
#: legitimately -- an ordering really can be nonsense -- so this group needs an
#: attribution cue before it counts.
DISMISSAL = "dismissal"

#: Spoken-register markers: interjections, fillers, and the contractions of speech.
#: These are what a verbatim quote of a chat message looks like.
INFORMALITY = "informality"

#: Shouting and stacked punctuation. The weakest group, and the one most likely to
#: catch a heading or an identifier, which is why it needs an attribution cue too.
EMPHASIS = "emphasis"

#: Every group this module can report, in decreasing order of confidence.
TONE_GROUPS: Tuple[str, ...] = (VULGARITY, DISMISSAL, INFORMALITY, EMPHASIS)

#: The groups that stand on their own. Everything else needs an attribution cue.
_UNATTRIBUTED_GROUPS: FrozenSet[str] = frozenset({VULGARITY})

_LEXICONS: Dict[str, str] = {
    VULGARITY: (
        r"fuck\w*|shit\w*|bullshit|crap\w*|damn\w*|bitch\w*|piss(?:ed|ing)?|arse\w*"
        r"|asshole\w*|wtf|stfu|bollocks|wanker\w*|prick|dickhead"
    ),
    DISMISSAL: (
        r"stupid\w*|idiot\w*|moron\w*|dumb|garbage|trash|rubbish|ridiculous|absurd"
        r"|pathetic|useless|awful|terrible|horrible|hate[ds]?|hating|lazy|sloppy"
        r"|incompetent|nonsense|insane|crazy|disaster|bloody|worthless|clueless"
    ),
    INFORMALITY: (
        # `whatever` earns a lookahead the others do not: as a relative pronoun it is
        # ordinary technical prose ("whatever the guard consults"), and only the
        # standalone, sentence-final use is the dismissive interjection this group is
        # about. Both readings are common in this corpus, so the distinction is the
        # difference between a useful signal and a word that fires on everything.
        r"yeah|yep|yup|nope|nah|ugh|meh|lol|lmao|omg|dude|jeez|geez"
        r"|whatever(?=\s*(?:[.,;:!?]|$))|seriously"
        r"|honestly|gonna|gotta|wanna|kinda|sorta|dunno|ain't|y'all|suck|sucks|sucked"
        r"|sucking|freaking|frickin\w*|damned|blah|oof|ouch|yikes|wow"
    ),
}

_PATTERNS: Dict[str, re.Pattern[str]] = {
    group: re.compile(r"\b(?:%s)\b" % lexicon, re.IGNORECASE)
    for group, lexicon in _LEXICONS.items()
}

#: Stacked terminal punctuation, or three or more consecutive shouted words.
#:
#: The thresholds are where this corpus put them. One shouted word is an identifier
#: (``YAML``, ``HEAD``); two in a row is a heading or a status label (a verdict written
#: in capitals, a section title), which is why two is not enough; three in a row is a
#: voice. Stacked punctuation needs no run at all -- it does not occur in this
#: repository's technical prose inside a quotation, and it is what a reaction looks
#: like. The cost of the three-word bar is stated in the module docstring: a two-word
#: shout quoted verbatim passes, and has to be caught by a reader.
_EMPHASIS = re.compile(r"!!|\?!|!\?|\b[A-Z][A-Z']+\b(?:[ ]+\b[A-Z][A-Z']+\b){2,}")

#: Words that mark the text after them as something a person said or wrote. Matched in
#: the 200 characters before a quotation, which is where a reporting clause lives.
ATTRIBUTION_CUES: Tuple[str, ...] = (
    "said",
    "says",
    "saying",
    "told",
    "tells",
    "asked",
    "asks",
    "wrote",
    "writes",
    "put it",
    "puts it",
    "called it",
    "calls it",
    "described",
    "describes",
    "replied",
    "responded",
    "answered",
    "complained",
    "objected",
    "pushed back",
    "reacted",
    "reaction",
    "feedback",
    "verdict",
    "remark",
    "remarked",
    "commented",
    "quote",
    "quoted",
    "in his words",
    "in her words",
    "in their words",
    "his words",
    "her words",
    "their words",
    "the owner",
    "the human",
    "the reviewer",
    "the requester",
    "the reporter",
)

_CUES = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(cue) for cue in ATTRIBUTION_CUES), re.I)

#: How far back a reporting clause may sit. Long enough for "X, who had already said
#: this twice, put it as", short enough that an unrelated earlier sentence does not
#: attribute a quotation three sentences later.
_ATTRIBUTION_WINDOW = 200

#: The spec fields that hold prose a person could be quoted in. ``context[].why`` and
#: ``acceptance[].text`` are deliberately absent: they are specifications rather than
#: reportage, and no quotation of a person has ever been written into one.
SPEC_PROSE_FIELDS: Tuple[str, ...] = (
    "summary",
    "intent",
    "description",
    "constraints",
    "out_of_scope",
)

#: Every addressable prose region of a task, in the spelling :func:`field_text` and
#: ``agentjobs redact`` both use. Log bodies are addressed as ``log[<id>].body``.
TASK_PROSE_FIELDS: Tuple[str, ...] = ("title", "ball_prompt") + tuple(
    f"spec.{name}" for name in SPEC_PROSE_FIELDS
)

#: How a log entry's body is addressed. Held here, next to the field names it completes,
#: so the manager's redaction verb and :func:`field_text` cannot disagree about the
#: spelling -- and exported compiled because ``manager`` uses ``re`` as a parameter name
#: and cannot import the module of that name without shadowing it.
LOG_BODY_FIELD = re.compile(r"log\[(\d+)\]\.body")

_QUOTE_SHAPES: Tuple[re.Pattern[str], ...] = (
    re.compile(r'"([^"\n]{2,600})"'),
    re.compile("“([^”\n]{2,600})”"),
    # A Markdown block quote: one or more consecutive lines opening with `>`. Captured
    # whole so a remark spread over three lines is one finding rather than three.
    re.compile(r"(?m)^(?:[ \t]*>[^\n]*\n?)+"),
)


@dataclass(frozen=True)
class QuotedRemark:
    """One quotation that looks like a person's words rather than a paraphrase.

    ``text`` is the quoted run itself. It is here so an author can see what fired and
    fix it, and it is the one field that must never reach anything durable: the
    importer and the store record ``field``, ``groups`` and ``offset`` and not this.
    """

    field: str
    text: str
    groups: Tuple[str, ...]
    attributed: bool
    offset: int

    def locator(self) -> str:
        """Where the remark is, with no part of the remark in it.

        The form used everywhere a finding is written down rather than shown to the
        person who can fix it -- a quarantine row, an exception message, a report.
        """
        return f"{self.field} at offset {self.offset} ({', '.join(self.groups)})"

    def message(self) -> str:
        """What to tell the author who just wrote this, at the moment they wrote it."""
        shown = self.text.strip()
        if len(shown) > 80:
            shown = shown[:77] + "..."
        return (
            f"{self.field} quotes a person verbatim: “{shown}”. A task record "
            "states what somebody meant, not the words they used, and never reproduces "
            "the tone of a remark -- this repository's records are public, where a "
            "quoted aside reads as a characterisation of a real person rather than as "
            "engineering. Say what was meant instead; keep the substance. Quote only "
            "where the exact wording is the subject, such as an API name or a spec "
            "sentence being disputed."
        )


def speakers_in(task: "Task") -> FrozenSet[str]:
    """Names this record could plausibly be quoting, taken from the record itself.

    The people who appear in a task are the people whose words might be quoted in it,
    so the record supplies its own speaker list and this module needs no project
    configuration to work. Multi-word actors contribute each of their name parts, so a
    log written by ``Ada Lovelace`` makes ``Ada`` an attribution cue too.
    """
    names: set[str] = set()
    candidates = [entry.actor for entry in task.log]
    candidates.append(task.assignment.owner or "")
    for candidate in candidates:
        for part in re.split(r"[^A-Za-z]+", candidate or ""):
            if len(part) > 2:
                names.add(part.lower())
    return frozenset(names)


def _tone_groups(quote: str) -> Tuple[str, ...]:
    """The tone groups a quoted run trips, in :data:`TONE_GROUPS` order."""
    fired = [
        group for group in (VULGARITY, DISMISSAL, INFORMALITY) if _PATTERNS[group].search(quote)
    ]
    if _EMPHASIS.search(quote):
        fired.append(EMPHASIS)
    return tuple(fired)


def _attributed(text: str, start: int, speakers: Iterable[str]) -> bool:
    """Whether a reporting clause or a known name sits just before ``start``."""
    window = text[max(0, start - _ATTRIBUTION_WINDOW) : start]
    if _CUES.search(window):
        return True
    lowered = window.lower()
    return any(re.search(r"\b%s\b" % re.escape(name), lowered) for name in speakers)


def _quotations(text: str) -> List[Tuple[int, str]]:
    """Every quotation-shaped run in ``text``, as ``(offset, quoted text)``.

    Overlapping shapes are collapsed by offset so a block quote holding a double-quoted
    phrase is reported once, at the outer shape, rather than twice.
    """
    found: Dict[int, str] = {}
    for shape in _QUOTE_SHAPES:
        for match in shape.finditer(text):
            quoted = match.group(1) if match.groups() else match.group(0)
            found.setdefault(match.start(), quoted)
    return sorted(found.items())


def scan_text(
    text: Optional[str], *, field: str, speakers: Iterable[str] = ()
) -> List[QuotedRemark]:
    """Every quoted remark in one prose field."""
    if not text:
        return []
    speaker_set = frozenset(speakers)
    remarks: List[QuotedRemark] = []
    for offset, quoted in _quotations(text):
        groups = _tone_groups(quoted)
        if not groups:
            continue
        attributed = _attributed(text, offset, speaker_set)
        if not attributed and not set(groups) & _UNATTRIBUTED_GROUPS:
            continue
        remarks.append(
            QuotedRemark(
                field=field,
                text=quoted,
                groups=groups,
                attributed=attributed,
                offset=offset,
            )
        )
    return remarks


def field_text(task: "Task", field: str) -> Optional[str]:
    """Read one addressable prose region, in the ``agentjobs redact`` spelling.

    Returns ``None`` for a field this task does not have -- an absent ``ball_prompt``,
    a log id that is not in the log -- so a caller can tell "empty" from "no such
    field" only by asking whether the name is addressable at all.
    """
    if field in ("title", "ball_prompt"):
        return getattr(task, field, None)
    if field.startswith("spec."):
        name = field[len("spec.") :]
        if name in SPEC_PROSE_FIELDS:
            return getattr(task.spec, name, None)
        return None
    match = LOG_BODY_FIELD.fullmatch(field)
    if match is None:
        return None
    wanted = int(match.group(1))
    for entry in task.log:
        if entry.id == wanted:
            return entry.body
    return None


def task_prose(task: "Task") -> List[Tuple[str, Optional[str]]]:
    """Every addressable prose region of a task, paired with its text."""
    regions: List[Tuple[str, Optional[str]]] = [
        (name, field_text(task, name)) for name in TASK_PROSE_FIELDS
    ]
    regions.extend((f"log[{entry.id}].body", entry.body) for entry in task.log)
    return regions


def scan_task(task: "Task", *, fields: Optional[Sequence[str]] = None) -> List[QuotedRemark]:
    """Every quoted remark in a task, or in just the named regions of it.

    ``fields`` narrows the scan to what one write could have touched, which is how
    :mod:`agentjobs.record_check` stays silent on a verb that did not write the region
    a quotation is sitting in.
    """
    speakers = speakers_in(task)
    regions = task_prose(task)
    if fields is not None:
        wanted = set(fields)
        regions = [region for region in regions if region[0] in wanted]
    remarks: List[QuotedRemark] = []
    for name, text in regions:
        remarks.extend(scan_text(text, field=name, speakers=speakers))
    return remarks
