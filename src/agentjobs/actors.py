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

Resolving *which* person is acting used to be impossible, so a config naming two of them
was refused outright (task-064). It is possible now: task-329 resolves who is asking and
:mod:`agentjobs.identities` maps a proven login to one of these ids, which is why
:func:`human_identity` takes a principal and why several ``kind: human`` entries are
accepted. Retirement arrives with it -- see :attr:`Actor.retired`, which changes what may
be written and nothing about what can be read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .identities import IdentityRegistry, unmapped_login_help
from .principals import Principal

HUMAN = "human"
AGENT = "agent"

DISPATCHER = "dispatcher"
"""The actor id the dispatcher writes as.

Design section 9 requires every *forced* ball move -- a run that ended without handing
off, a session parked on a permission prompt, a budget cap that refused a run -- to be
attributed to the dispatcher rather than to the agent, because the agent did not do it.
Reserved rather than left to each project's ``actors:``: a project that had not added it
would see dispatch fail at exactly the moment it was trying to report a failure.
"""


@dataclass(frozen=True)
class Actor:
    """One actor id, with the kind config says it is."""

    id: str
    kind: str
    display_name: str
    retired: bool = False
    """True when this actor may no longer act, set by ``retired: true`` in config.

    A retired actor stays in the vocabulary. That is the whole mechanism: the log is
    append-only, so every entry naming them has to keep resolving to a displayable
    name and a kind forever, and deleting the entry would leave those entries pointing
    at nothing. Retirement therefore changes exactly one thing -- :func:`validate_actor`
    refuses new writes as them -- and changes nothing about reading history.
    """

    @property
    def is_human(self) -> bool:
        return self.kind == HUMAN

    @property
    def can_act(self) -> bool:
        """True when a new record may be attributed to this actor."""
        return not self.retired


class UnknownActorError(ValueError):
    """An actor id that config does not define.

    Raised rather than tolerated, per D2: an unrecognised id written into a log entry is
    a silent no-op that survives forever, and the log is the one place in this system
    that is never rewritten.
    """


class RetiredActorError(UnknownActorError):
    """An actor config defines but has retired, so nothing may be written as them.

    A subclass, and deliberately so: every caller that already refuses an unwritable
    actor id -- the two API routes, and anything downstream of them -- refuses this one
    too, with its own message, without a line of plumbing. The distinction still exists
    for a caller that wants it, because "who is that" and "they have left" need
    different guidance, which is the same reasoning that splits ``UNCONFIGURED`` from
    the identity problems below.
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
            retired=bool(entry.get("retired") or False),
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
    """Every person config knows about, retired ones included.

    Retired people are *in* this list on purpose: a reader resolving an old log entry
    needs them, and every caller that means "people who may act now" says so by using
    :func:`active_humans`. The membership question and the permission question are
    different, and collapsing them is how a retired person's history stops rendering.
    """
    return [actor for actor in load_actors(config).values() if actor.is_human]


def active_humans(config: Dict[str, Any]) -> List[Actor]:
    """The people who may still act."""
    return [actor for actor in humans(config) if actor.can_act]


UNCONFIGURED = "unconfigured"
"""Nobody at all is configured. Add yourself."""

UNMAPPED = "unmapped"
"""A proven login that maps to no actor. Add the mapping; never guess one."""

RETIRED = "retired"
"""A login that maps to somebody who has stopped acting on this project."""

UNKNOWN_ACTOR = "unknown_actor"
"""A login mapped to an id this project's ``actors:`` does not define."""

AMBIGUOUS = "ambiguous"
"""Several people configured, and nothing said which of them is asking.

The successor to the old ``MULTIPLE`` refusal, and narrower than it: ``MULTIPLE``
refused *any* multi-human config outright, while this refuses only a request that
arrived with no identity to resolve and no stated machine owner. A remote caller whose
login is mapped is not ambiguous however many people are configured, which is the whole
point of task-330.
"""

NOT_A_PERSON = "not_a_person"
"""The caller is a dispatched run. A run is never attributed as a human, ever."""

MULTIPLE = AMBIGUOUS
"""Deprecated alias for :data:`AMBIGUOUS`, kept so an older caller still resolves.

The literal string changes with it. Nothing outside this repository is known to match
on it, and a stale ``"multiple"`` comparison silently classifying the new refusals as
the old one is a worse failure than an import that breaks loudly.
"""


PROBLEM_HEADLINES: Dict[str, str] = {
    UNCONFIGURED: "No user configured.",
    UNMAPPED: "Your login is not mapped.",
    RETIRED: "That person has retired.",
    UNKNOWN_ACTOR: "Mapped to an actor this project does not define.",
    AMBIGUOUS: "Cannot tell who is asking.",
    NOT_A_PERSON: "This is not a person.",
}
"""A short headline per problem, for a surface that shows one above the detail.

Here rather than only in each UI because there are two of them -- the React app and the
legacy server-rendered page -- and the failure mode is silent: an unrecognised code gets
whatever the fallback says, which before task-330 was "No user configured" and would have
sent somebody whose login was simply unmapped to edit a file that was already correct.
``tests/test_identity_problem_headlines.py`` fails when a problem code has no entry here
or in ``frontend/src/components/identityProblem.ts``.
"""


@dataclass(frozen=True)
class Identity:
    """Who the GUI acts as, or why it cannot tell."""

    user: Optional[str] = None
    problem: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.user is not None


def _identity_registry(registry: Optional[IdentityRegistry]) -> IdentityRegistry:
    """The passed registry, or the machine's, read fresh."""
    return registry if registry is not None else IdentityRegistry()


def _resolved(config: Dict[str, Any], actor_id: str, *, via: str) -> Identity:
    """Turn a mapped actor id into an identity, or say why it cannot act.

    The two refusals here are the ones a mapping can produce that the *mapping* is not
    at fault for: the project may not define the id, and it may have retired it. Both
    name the project's config, because that is the file that has to change.
    """
    known = load_actors(config)
    if not known:
        # A fresh `agentjobs init` defines no vocabulary at all. `validate_actor`
        # accepts any id there for the same reason: refusing would make the product
        # unusable before it is configured, which is a worse failure than a typo on a
        # project that has not decided who its actors are.
        return Identity(user=actor_id)
    actor = known.get(actor_id)
    if actor is None:
        names = ", ".join(sorted(known)) or "(none)"
        return Identity(
            problem=UNKNOWN_ACTOR,
            detail=(
                f"{via} maps to actor {actor_id!r}, which this project does not define. "
                f"Its configured actors are: {names}. Add an entry with 'kind: human' "
                "to 'actors:' in .agentjobs/config.yaml, or point the mapping at an id "
                "that is already there."
            ),
        )
    if not actor.can_act:
        return Identity(
            problem=RETIRED,
            detail=(
                f"{via} maps to actor {actor_id!r}, who is retired and may no longer "
                "act. Their past entries still resolve and render -- retirement never "
                "rewrites the log. To bring them back, remove 'retired: true' from "
                "their entry in 'actors:' in .agentjobs/config.yaml."
            ),
        )
    return Identity(user=actor.id)


def human_identity(
    config: Dict[str, Any],
    principal: Optional[Principal] = None,
    *,
    identities: Optional[IdentityRegistry] = None,
) -> Identity:
    """Resolve the acting human for this request, or say precisely why there isn't one.

    **This is per request, not per project.** It used to be a pure function of config,
    which is why a config naming two people had no honest answer and was refused
    outright (task-064). ``principal`` -- task-329's resolution of who is asking -- is
    what makes several people answerable: a remote caller arrives with a login the
    front door proved, and :mod:`agentjobs.identities` says which configured actor that
    login is.

    The order, and why each step is not interchangeable:

    1.  **A run is not a person.** Refused first, before anything can map it, because a
        dispatched agent that resolved to a human would be the impersonation this epic
        exists to close.
    2.  **A proven login** is looked up in the machine's identity registry. Mapped to a
        live actor, that is the answer whatever else config contains. Mapped to
        nothing, it is *refused* with the whole of what a second person needs in order
        to fix it -- and it never falls through to ``default_user``, which is the
        defect task-064 removed.
    3.  **Anything else** -- the person at this machine on bare loopback, or a caller
        with no request context at all, such as the CLI -- is the machine's owner. The
        registry's ``owner:`` names them. Failing that, a project with several people
        who can act is :data:`AMBIGUOUS` and is refused; a project with at most one is
        resolved from ``default_user`` or from that lone person, which is exactly what
        every such caller got before principals existed.

    Neither surviving fallback is the banned one. The banned fallback is substituting a
    default for a login that *was* proven and did not map, which claims one person's act
    for another. A bare-loopback caller proved no login at all; the machine owner is a
    fact about this machine stated in advance rather than a guess made at the moment of
    the write; and where the project has only one person there is nothing to guess
    between.
    """
    if principal is not None and principal.is_run:
        return Identity(
            problem=NOT_A_PERSON,
            detail=(
                f"This request is a dispatched run ({principal.run_id or 'unnamed'}), "
                "not a person. A run is never recorded as a human, and no configuration "
                "changes that."
            ),
        )

    if principal is not None and principal.login:
        login = principal.login
        registry = _identity_registry(identities)
        mapped = registry.resolve(login)
        if mapped is None:
            return Identity(problem=UNMAPPED, detail=unmapped_login_help(login, home=registry.home))
        return _resolved(config, mapped.actor_id, via=f"The login {login!r}")

    registry = _identity_registry(identities)
    active = active_humans(config)

    if registry.owner:
        return _resolved(config, str(registry.owner), via="The machine owner")

    if len(active) > 1:
        # **``default_user`` does not rescue this, and is not consulted.** task-064's
        # reasoning survives task-330 intact on this path: project config is committed
        # and travels with a clone, so it cannot name who is at *this* keyboard, and
        # honouring it here would confidently record one person's approval as another's.
        # What has changed is that there is now a place that can answer -- the machine's
        # own ``owner:``, checked directly above -- so the refusal names it.
        names = ", ".join(sorted(person.id for person in active))
        return Identity(
            problem=AMBIGUOUS,
            detail=(
                f"{len(active)} people can act on this project ({names}) and this "
                "request carried no proven identity, so AgentJobs cannot tell which of "
                "them is asking and will not guess. Reach the dashboard through the "
                "tailnet front door, which proves who you are, or -- if you are the "
                f"person at this machine -- name yourself as 'owner:' in {registry.path}."
            ),
        )

    configured = config.get("default_user")
    if configured:
        # Deliberately *not* put through `_resolved`. `default_user` predates this
        # module's vocabulary check and installs exist whose default names an id
        # `actors:` never listed; `validate_actor` already refuses the write in that
        # case, so re-refusing here would break configs that work today for no gain.
        # Retirement is the one thing that must still bite, because a retired person
        # not being able to act is the point of retiring them.
        actor = load_actors(config).get(str(configured))
        if actor is not None and not actor.can_act:
            return _resolved(config, str(configured), via="'default_user'")
        return Identity(user=str(configured))

    if len(active) == 1:
        return Identity(user=active[0].id)

    return Identity(
        problem=UNCONFIGURED,
        detail=(
            "No human actor is configured, so an action taken here could not say who "
            "took it. Add an entry with 'kind: human' to 'actors:' in "
            ".agentjobs/config.yaml and set 'default_user:' to its id."
        ),
    )


def default_user(
    config: Dict[str, Any],
    principal: Optional[Principal] = None,
    *,
    identities: Optional[IdentityRegistry] = None,
) -> Optional[str]:
    """The id the GUI acts as, or None when it cannot be resolved."""
    return human_identity(config, principal, identities=identities).user


FINISHER = "finisher"
"""The actor id the scripted post-approval finish writes as (task-241).

Distinct from ``dispatcher`` because it is a different claim about who did something. A
``dispatch`` entry says AgentJobs started an agent; a ``finisher`` entry says AgentJobs
rebased, ran the gate and merged, with no agent anywhere in it. Collapsing the two would
make the record unable to answer "was a model involved in this merge", which is the one
question this whole mechanism exists to change the answer to.

Reserved and an agent, like ``dispatcher`` and for the same reason: an entry it writes
must never clock a dispatch as a human act.
"""

RESERVED = {
    DISPATCHER: Actor(id=DISPATCHER, kind=AGENT, display_name="AgentJobs dispatcher"),
    FINISHER: Actor(id=FINISHER, kind=AGENT, display_name="AgentJobs finisher"),
}
"""Actor ids AgentJobs itself writes as, valid in every project without configuration.

Kept out of ``load_actors`` on purpose, for two reasons. Merging them in would mean the
configured vocabulary is never empty, which silently removes the "a fresh init accepts
any id" allowance below and would reject every real actor on an unconfigured project.
And a reserved id is not a choice: it should not appear in a picker of who is acting,
because nobody may act as it.
"""


def reserved_actors() -> Dict[str, Actor]:
    """The reserved ids, for a UI that needs to resolve one's kind for display."""
    return dict(RESERVED)


def actor_kinds(config: Dict[str, Any]) -> Dict[str, str]:
    """Every id a write can be attributed to, mapped to ``human`` or ``agent``.

    For a reader that has an actor id out of a log entry and needs to know which kind
    wrote it -- the queue listing's move provenance is the first such reader. It is a
    plain mapping rather than :class:`Actor` objects because that is all such a reader
    wants, and because it travels through the manager, which has no config of its own.

    **Reserved ids are merged last and therefore win**, the same rule and the same
    reason as ``dispatch.guards.actor_kind``: a project that wrote ``dispatcher:
    {kind: human}`` into its ``actors:`` would otherwise have every dispatcher-written
    entry read as a human act. An id config does not define is simply absent, and a
    caller that finds nothing here knows only that -- it must not read the absence as
    either kind.
    """
    kinds = {actor.id: actor.kind for actor in load_actors(config).values()}
    kinds.update({actor.id: actor.kind for actor in RESERVED.values()})
    return kinds


def validate_actor(config: Dict[str, Any], actor_id: str) -> str:
    """Return the id if config defines it or AgentJobs reserves it, else raise.

    When config defines no actors at all -- a fresh ``agentjobs init`` that has not been
    edited -- any id is accepted. Validating against an empty vocabulary would reject
    everything and make the product unusable before it is configured, which is a worse
    failure than a typo'd actor on a project that has not decided who its actors are.
    """
    if actor_id in RESERVED:
        return actor_id
    known = load_actors(config)
    if not known:
        return actor_id
    actor = known.get(actor_id)
    if actor is None:
        names = ", ".join(sorted(actor.id for actor in known.values() if actor.can_act)) or "(none)"
        raise UnknownActorError(
            f"'{actor_id}' is not an actor in this project. Configured actors: {names}. "
            "Add it to 'actors:' in .agentjobs/config.yaml, or use a configured id."
        )
    if not actor.can_act:
        # The only thing retirement does. Reading is untouched -- the id is still in the
        # vocabulary above, so `load_actors`, `actor_kinds` and every renderer of an old
        # log entry keep resolving it to a name and a kind, which is what "the log is
        # append-only" means in practice.
        raise RetiredActorError(
            f"'{actor_id}' is retired and may no longer act. Their existing entries are "
            "unaffected and still resolve. To bring them back, remove 'retired: true' "
            "from their entry in 'actors:' in .agentjobs/config.yaml."
        )
    return actor_id
