"""The subscription as a start gate: clickless takeoffs pause on a usage limit (task-463).

Once slots stay full, the thing that stops agents running is not the ceiling, the caps or
the queue -- it is the subscription. task-417 already recovers a run that hits the limit
*while it is running*: the session is parked ``external``/``service``, an incident is
opened naming the reset, a probe claims it after the reset and wakes the session in
place. What it never had a view on is the two starters that have nobody watching them.
The dispatch queue (task-459) and the pull mode (task-462) both start runs on a tick, and
into an open usage limit each start is a park: the session comes up, asks for a turn, is
refused, and joins the incident. The hourly dispatch cap is spent on runs that did no
work, the tasks collect a park note apiece, and the board says nothing about why.

**This module answers one question and starts nothing.** Given the machine and a
prospective start, is there an open incident against the credential that start would
spend? The answer holds the two clickless starters off, and the same answer -- with the
incident, the runner and the reset time in it -- is what ``/api/runs/live`` carries to
the slot board.

Three properties are the whole design, and each was a live alternative:

* **It owns no lifecycle.** It opens no incident, runs no probe and closes nothing.
  task-417's probe already decides when an incident is over, and a second mechanism
  deciding the same thing is two clocks that will disagree at the moment it matters. So
  resume is not a mechanism here at all: the gate reads the book every tick, and a closed
  incident is simply a tick that no longer finds one. Nothing has to be cleared, which is
  also why a server restart mid-pause resumes correctly with no state to recover.

* **It pauses; it does not disarm.** Until this task the pull mode *retired* an arming
  when it saw an open incident (``pull._incident_stall``), which was the honest thing to
  do while nothing could resume -- but it threw away the authority, so the hours after
  the reset, which are exactly the hours the arming was for, were lost unless a person
  came back and armed it again. A paused arming keeps its bound and its authority and
  starts again on the first tick after the incident closes.

* **It is per credential, not per machine.** See
  :attr:`~agentjobs.dispatch.auth_recovery.Profile.credential_key`: a Codex incident must
  not ground a Claude runner, and a usage limit charged to one Claude home says nothing
  about a different one. The model is deliberately *not* part of it -- a subscription's
  window is charged against the login, so the limit that refused Opus refuses Sonnet on
  the same home too.

**Every open kind pauses, not only ``usage_limit``** -- decided rather than assumed, and
recorded as such on task-463. ``auth`` means the credential cannot log in and ``spend``
means it will not be billed; a run started into either parks exactly as it does into a
usage limit, so the case for holding is the same case. What differs is the *promise the
board may make*: only a usage limit carries a reset, so the other two are rendered as
"until the incident clears" and never as a time. This is narrower than the behaviour it
replaces, which retired every arming on the machine for any incident on any credential.

**A manual click is untouched.** ``guards.dispatch_task`` never consults this module, so
a person who clicks Dispatch during an incident still gets the run, and still gets
task-417's park a minute later. That asymmetry is the point rather than an oversight: the
park is a message, and a person who just clicked is there to read it and to decide
whether to wait. A tick is not there, cannot read it, and would only produce more of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from agentjobs.dispatch import auth
from agentjobs.dispatch.auth_recovery import AUTH_ENV_NAMES, Incident, Profile, book_for
from agentjobs.dispatch.config import (
    DispatchError,
)
from agentjobs.dispatch.config import DispatchRunner as RunnerDefinition
from agentjobs.dispatch.config import load_dispatch_config, resolve_runner

__all__ = [
    "Pause",
    "credential_for_runner",
    "profile_for_runner",
    "open_pauses",
    "pause_for",
    "paused_starts",
]


KIND_WORDS: Dict[str, str] = {
    "usage_limit": "usage limit",
    "auth": "login",
    "spend_limit": "spend limit",
}
"""How each incident kind reads in a sentence a person sees on the board."""


@dataclass(frozen=True)
class Pause:
    """One open incident, as a reason a tick started nothing.

    ``runner`` is the runner this pause was matched *against* -- the one the prospective
    start would have used -- rather than a property of the incident, which knows only a
    credential. Two runners on one login therefore report the same incident under their
    own names, which is what a person reading the board wants: they are looking at a row,
    not at a credential digest.
    """

    incident_id: str
    kind: str
    credential_key: str
    runner: str
    resets_at: Optional[datetime]
    opened_at: datetime

    @property
    def kind_word(self) -> str:
        return KIND_WORDS.get(self.kind, self.kind.replace("_", " "))

    @property
    def resumes_by_itself(self) -> bool:
        """Whether the reset that ends this will arrive without a person.

        A usage limit does. A dead login and a spend limit do not, and a board that
        counted down to a reset for either would be promising something nobody has
        undertaken to do.
        """
        return self.kind == "usage_limit" and self.resets_at is not None

    def sentence(self) -> str:
        """Why nothing started, in the words the tick's report and the API both use.

        The time is UTC here on purpose. This string is read by a log reader and by the
        CLI, both of which are already in UTC; the board is given ``resets_at`` as a
        field and renders it in the reader's own zone, because "6:40pm" is only useful
        to somebody in the zone it was said in.
        """
        if self.resumes_by_itself and self.resets_at is not None:
            when = f"until {self.resets_at.isoformat()}"
        else:
            when = "until the incident clears"
        return (
            f"paused {when}: {self.kind_word} on {self.runner} "
            f"({self.incident_id}). Nothing starts on that credential; the queue keeps "
            "its entries and an arming keeps its bound."
        )


def profile_for_runner(
    definition: RunnerDefinition, *, claude_home: Optional[Path] = None
) -> Profile:
    """The credential half of the profile a run started with this runner would open.

    Built from the configuration rather than from a live
    :class:`~agentjobs.dispatch.runner.DispatchRunner`, because there is no run here to
    build one for and constructing one needs a manager and a project root that a gate has
    no business resolving. The inputs are exactly the ones ``auth_recovery.profile_for``
    reads off a *started* run -- ``os.environ`` updated with the runner's own ``env``,
    and ``auth.claude_home`` on the ambient setting -- so a pause computed here and an
    incident opened there agree about whose quota is at stake.

    ``executable`` and ``model`` are left empty because
    :attr:`~agentjobs.dispatch.auth_recovery.Profile.credential_key` does not read them.
    This is **not** a profile to probe with; it is a profile to compare credentials with.

    One known limit, and it is inherited rather than introduced: a runner that overrides
    the Claude home through its own ``env`` is not distinguished from one that does not,
    because ``profile_for`` does not distinguish them either -- the home it records is
    the dispatcher's ambient one. Matching what the book actually writes is what makes
    the gate correct; diverging to be cleverer would make it miss.
    """
    environment = dict(os.environ)
    environment.update(definition.env)
    return Profile(
        driver=definition.driver.value,
        executable=(),
        model=None,
        claude_home=str(auth.claude_home(claude_home)),
        auth_env=tuple(name for name in AUTH_ENV_NAMES if environment.get(name)),
    )


def credential_for_runner(
    definition: RunnerDefinition, *, claude_home: Optional[Path] = None
) -> str:
    """The credential key a run started with this runner would spend."""
    return profile_for_runner(definition, claude_home=claude_home).credential_key


def _runner_for(
    home: Path, project_id: str, *, runner: Optional[str], group: Optional[str]
) -> Optional[RunnerDefinition]:
    """Which runner this prospective start would use, or ``None`` when that is unknowable.

    ``None`` means **do not pause**, and that default is deliberate. A gate that cannot
    tell whose quota a start would spend has learned nothing about whether it is refused,
    and holding on that would stop a machine for the one condition the gate is least sure
    about. The real gates in ``dispatch_task`` run either way and refuse for their own
    reasons, with their reasons written down.

    A group is resolved the way the dispatch would resolve it *now*. It does not fail
    over to a different runner in the group because one credential is limited; that is a
    question about runner selection rather than about starting, and widening this gate
    into it would make an incident silently change which model does the work.
    """
    config = load_dispatch_config(home)
    if config is None:
        return None
    if runner:
        named = config.runners.get(runner)
        if named is not None:
            return named
    try:
        return resolve_runner(config, config.project(project_id), group=group).runner
    except (DispatchError, KeyError, ValueError):
        return None


def open_pauses(home: Path) -> Dict[str, Incident]:
    """Every open incident on this machine, keyed by the credential it is charged to.

    One read per tick, handed to each prospective start rather than re-read for each,
    because a tick asks this question once per queued entry and once per armed project
    and the answer cannot change between them.

    Never raises. An execution store that cannot be read is not evidence of an incident,
    and pausing every takeoff on the strength of an unreadable table would be a machine
    that stopped for the wrong reason -- the same argument ``pull._incident_stall`` made
    for the disarm this replaces.
    """
    try:
        incidents = book_for(home).open_incidents()
    except Exception:  # noqa: BLE001 - see the docstring
        return {}
    found: Dict[str, Incident] = {}
    for incident in incidents:
        # Oldest first out of the book, and the oldest is the one kept: it is the
        # incident whose reset a person has already been told about, and the one whose
        # probe is furthest along.
        found.setdefault(incident.profile.credential_key, incident)
    return found


def pause_for(
    home: Path,
    project_id: str,
    *,
    runner: Optional[str] = None,
    group: Optional[str] = None,
    incidents: Optional[Mapping[str, Incident]] = None,
) -> Optional[Pause]:
    """The open incident holding this prospective start off, or ``None`` to go ahead.

    ``incidents`` is the tick's one reading of the book; omitting it reads the book here,
    which is what a single caller -- the API, a test -- wants.
    """
    book = open_pauses(home) if incidents is None else incidents
    if not book:
        return None
    definition = _runner_for(home, project_id, runner=runner, group=group)
    if definition is None:
        return None
    key = credential_for_runner(definition)
    incident = book.get(key)
    if incident is None:
        return None
    return Pause(
        incident_id=incident.incident_id,
        kind=incident.kind,
        credential_key=key,
        runner=definition.name,
        resets_at=incident.resets_at,
        opened_at=incident.opened_at,
    )


def paused_starts(
    home: Path, candidates: Sequence[Tuple[str, Optional[str], Optional[str]]]
) -> List[Tuple[Tuple[str, Optional[str], Optional[str]], Optional[Pause]]]:
    """Judge several prospective starts against one reading of the book.

    Each candidate is ``(project_id, runner, group)``. Used by the API, which has to say
    which queued entries and which armed projects are waiting on which incident and would
    otherwise read the book once per row.
    """
    book = open_pauses(home)
    return [
        (
            candidate,
            pause_for(home, candidate[0], runner=candidate[1], group=candidate[2], incidents=book),
        )
        for candidate in candidates
    ]
