"""Machine-level map from a proven login to the actor id AgentJobs records.

task-329 established *who is asking* -- a :class:`~agentjobs.principals.Principal`
carrying the raw login the front door proved. It deliberately stopped there, leaving
``actor_id`` ``None``, because guessing which configured person a login belongs to
would write an attribution nobody chose. This module is the mapping it stopped short
of.

**The mapping is machine-level, in ``~/.agentjobs/identities.yaml``, and the actor
vocabulary stays per project.** A tailnet login is a property of this machine's
tailnet, not of any repository: one person has one login across every project this
server serves, so recording it once beside the project registry is the shape that
matches the fact. The rejected alternative and its reasoning are on task-330 as a
decision entry; the short form is that ``.agentjobs/config.yaml`` is committed and
travels with a clone, so putting logins there publishes a machine's account list to
everyone who clones the repository and forces the same person to be re-declared in
every project.

**An unmapped login is refused, never defaulted.** :func:`IdentityRegistry.resolve`
returns ``None`` and the caller says so; nothing here substitutes ``default_user``.
That fallback is exactly the defect task-064 removed -- attributing one person's
approval to another -- and re-introducing it under a registry would undo that task
while appearing to extend it.

The file:

.. code-block:: yaml

    owner: jeffposey        # who the person at this machine is, when several are configured
    identities:
      - login: jeff@example.com
        actor: jeffposey

Retirement is *not* here. It lives on the actor in project config
(:mod:`agentjobs.actors`), because "this person no longer acts on this project" is a
project fact, and because leaving the retired id in the project's vocabulary is what
keeps their past log entries resolving to a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .projects import default_home

IDENTITIES_FILENAME = "identities.yaml"
"""Beside ``projects.yaml`` in the registry home, and machine-local for the same reason:
both record what this particular machine has, and neither belongs in a repository."""


@dataclass(frozen=True)
class MappedIdentity:
    """One proven login and the actor id it records as."""

    login: str
    actor_id: str


class IdentityRegistry:
    """The login-to-actor map, read from ``~/.agentjobs/identities.yaml``.

    Read on construction rather than cached across requests: it is a few lines of
    hand-edited YAML, and a stale answer after adding yourself to it is precisely the
    "why is it still refusing me" that costs an afternoon.
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else default_home()
        self.path = self.home / IDENTITIES_FILENAME
        self._entries, self._owner = self._read()

    # ----- persistence -------------------------------------------------------

    def _read(self) -> tuple[Dict[str, MappedIdentity], Optional[str]]:
        """Parse the file, tolerating its absence and skipping unusable entries.

        A malformed entry is skipped rather than fatal: the file is hand-edited, and a
        typo in one person's line must not lock everybody else out of the dashboard.
        The refusal a skipped entry produces names the login that did not resolve,
        which is the same message an absent one produces and is equally actionable.
        """
        if not self.path.is_file():
            return {}, None
        try:
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return {}, None
        if not isinstance(loaded, dict):
            return {}, None

        entries: Dict[str, MappedIdentity] = {}
        for raw in loaded.get("identities") or []:
            identity = _coerce(raw)
            if identity is not None:
                entries[_key(identity.login)] = identity

        owner = loaded.get("owner")
        return entries, str(owner) if owner else None

    # ----- queries -----------------------------------------------------------

    @property
    def owner(self) -> Optional[str]:
        """The actor id for the person at this machine, or ``None`` if unstated.

        Answers the one question a login cannot: a caller on bare loopback is the
        machine's owner and presents no login at all, so with several people
        configured there is nothing to look them up by. Stated here rather than in
        project config because which human sits at this keyboard is a fact about the
        machine.
        """
        return self._owner

    def resolve(self, login: str) -> Optional[MappedIdentity]:
        """The mapping for a proven login, or ``None`` when there is none.

        Matched case-insensitively on the login: an email-shaped tailnet login is not
        case-sensitive in practice, and a refusal caused by capitalisation would be
        indistinguishable from a refusal caused by an unregistered person.
        """
        return self._entries.get(_key(login))

    def logins(self) -> List[str]:
        """Every mapped login, for a message that has to say what *is* known."""
        return sorted(identity.login for identity in self._entries.values())

    def actor_ids(self) -> List[str]:
        """Every actor id some login maps to."""
        return sorted({identity.actor_id for identity in self._entries.values()})


def _key(login: str) -> str:
    """The lookup key for a login."""
    return login.strip().casefold()


def _coerce(entry: Any) -> Optional[MappedIdentity]:
    """Read one ``identities:`` entry, or ``None`` if it names nothing usable."""
    if not isinstance(entry, dict):
        return None
    login = entry.get("login")
    actor = entry.get("actor") or entry.get("actor_id")
    if not login or not actor:
        return None
    return MappedIdentity(login=str(login).strip(), actor_id=str(actor).strip())


def identities_path(home: Optional[Path] = None) -> Path:
    """Where the map lives, for a message that has to name the file to edit."""
    base = Path(home).expanduser().resolve() if home else default_home()
    return base / IDENTITIES_FILENAME


def unmapped_login_help(login: str, *, home: Optional[Path] = None) -> str:
    """The whole onboarding experience for a second person, in one message.

    A refusal that says only "unrecognised" leaves the reader with a login, no file,
    and no shape. This names all three, because the person reading it has just been
    told they may not act and has nothing else to go on.
    """
    path = identities_path(home)
    return (
        f"The login {login!r} is not mapped to an actor, so an action taken here could "
        "not say who took it, and AgentJobs will not guess. Add it to "
        f"{path} (create the file if it is not there):\n"
        "\n"
        "  identities:\n"
        f"    - login: {login}\n"
        "      actor: <an id from 'actors:' in this project's .agentjobs/config.yaml>\n"
    )
