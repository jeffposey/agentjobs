"""Who acts: the config-resolved vocabulary of humans and agents.

D4 settled that a task file names an actor by bare id (``actor: claude``, ``owner:
jeff``) and that *kind* -- human or agent -- is resolved from config rather than
embedded in every log entry. This module is that resolution.

Config had an ``agents:`` list and no equivalent for people, so the GUI had nowhere to
look up who was reviewing and hardcoded ``user: 'human'`` in all three review buttons.
Every approval, change request and rejection was therefore recorded anonymously: the log
showed that a person acted, never which one. On a single-user project that reads as
harmless, but it defeats the record's central claim -- that a task file tells a
zero-context reader what happened and who did it -- for the half of the workflow humans
own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

HUMAN = "human"
AGENT = "agent"


@dataclass(frozen=True)
class Actor:
    """One actor id, with the kind config says it is."""

    id: str
    kind: str
    display_name: str

    @property
    def is_human(self) -> bool:
        return self.kind == HUMAN


class UnknownActorError(ValueError):
    """An actor id that config does not define.

    Raised rather than tolerated, per D2: an unrecognised id written into a log entry is
    a silent no-op that survives forever, and the log is the one place in this system
    that is never rewritten.
    """


def _coerce(entry: Any, default_kind: str) -> Optional[Actor]:
    """Read one config entry, accepting either a bare id or a mapping."""
    if isinstance(entry, str):
        return Actor(id=entry, kind=default_kind, display_name=entry)
    if isinstance(entry, dict):
        actor_id = entry.get("name") or entry.get("id")
        if not actor_id:
            return None
        return Actor(
            id=str(actor_id),
            kind=str(entry.get("kind") or default_kind),
            display_name=str(entry.get("display_name") or actor_id),
        )
    return None


def load_actors(config: Dict[str, Any]) -> Dict[str, Actor]:
    """Every actor config defines, keyed by id.

    Reads ``actors:`` -- one list carrying ``kind`` per entry, which is exactly what D4
    says config resolves -- and still reads a legacy ``agents:`` list, treating its
    entries as agents. Both are merged rather than one winning, so an install can adopt
    ``actors:`` for its people without rewriting its agents on the same day. ``actors:``
    takes precedence on an id defined in both.
    """
    actors: Dict[str, Actor] = {}
    for entry in config.get("agents") or []:
        actor = _coerce(entry, AGENT)
        if actor is not None:
            actors[actor.id] = actor
    for entry in config.get("actors") or []:
        actor = _coerce(entry, HUMAN)
        if actor is not None:
            actors[actor.id] = actor
    return actors


def humans(config: Dict[str, Any]) -> List[Actor]:
    """The people config knows about."""
    return [actor for actor in load_actors(config).values() if actor.is_human]


UNCONFIGURED = "unconfigured"
MULTIPLE = "multiple"


@dataclass(frozen=True)
class Identity:
    """Who the GUI acts as, or why it cannot tell."""

    user: Optional[str] = None
    problem: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.user is not None


def human_identity(config: Dict[str, Any]) -> Identity:
    """Resolve the acting human, or say precisely why there isn't one.

    Two failures, deliberately distinguished, because they need different guidance:
    nobody is configured (add yourself), or several people are (not supported yet).

    **Exactly one human is supported for now.** Several would need account management --
    who is at the keyboard, and how the server knows -- which is a real feature, not a
    config shape. Rather than half-support it by guessing or by silently taking the
    first, a multi-human config is refused with a message pointing at the task that
    covers it. Attributing one person's approval to another is the failure this whole
    module exists to prevent, so approximating here would be self-defeating.
    """
    people = humans(config)
    configured = config.get("default_user")

    if len(people) > 1:
        names = ", ".join(sorted(person.id for person in people))
        return Identity(
            problem=MULTIPLE,
            detail=(
                f"{len(people)} human actors are configured ({names}), and AgentJobs "
                "supports one. It cannot know which of you is acting, and recording the "
                "wrong person is worse than recording nobody. Leave one 'kind: human' "
                "entry in .agentjobs/config.yaml until account management lands."
            ),
        )
    if configured:
        return Identity(user=str(configured))
    if len(people) == 1:
        return Identity(user=people[0].id)
    return Identity(
        problem=UNCONFIGURED,
        detail=(
            "No human actor is configured, so an action taken here could not say who "
            "took it. Add an entry with 'kind: human' to 'actors:' in "
            ".agentjobs/config.yaml and set 'default_user:' to its id."
        ),
    )


def default_user(config: Dict[str, Any]) -> Optional[str]:
    """The id the GUI acts as, or None when it cannot be resolved."""
    return human_identity(config).user


def validate_actor(config: Dict[str, Any], actor_id: str) -> str:
    """Return the id if config defines it, else raise UnknownActorError.

    When config defines no actors at all -- a fresh ``agentjobs init`` that has not been
    edited -- any id is accepted. Validating against an empty vocabulary would reject
    everything and make the product unusable before it is configured, which is a worse
    failure than a typo'd actor on a project that has not decided who its actors are.
    """
    known = load_actors(config)
    if not known:
        return actor_id
    if actor_id not in known:
        names = ", ".join(sorted(known)) or "(none)"
        raise UnknownActorError(
            f"'{actor_id}' is not an actor in this project. Configured actors: {names}. "
            "Add it to 'actors:' in .agentjobs/config.yaml, or use a configured id."
        )
    return actor_id
