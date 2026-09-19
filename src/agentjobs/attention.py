"""Attention episodes: whether work has stopped on a person, and whether they know.

The badge in the header has said *how many* tasks are waiting since task-338, and it
says it only to somebody already looking at the page. This module is the half that has
to work when nobody is looking: it turns the waiting set into a small piece of durable
state that a desktop shell, and later a phone, can be driven from without either of
them inventing its own idea of "new".

**One predicate, one order.** The waiting set is
:func:`~agentjobs.dashboard.human_waiting_tasks` and nothing else, so a notification can
never disagree with the number on the page it links to. That was the whole finding of
``tests/test_attention_tiers.py``: three surfaces each computing "blocked on you" gave
three different answers, and the one that counted drafts never reached zero.

**An episode, not an event.** Alerting per task write is what makes people turn alerts
off. An episode opens when the waiting set goes from empty to non-empty and owes exactly
one interruptive notification; tasks joining an open, unacknowledged episode move the
count and owe nothing. The rule is stated in full on task-422 and repeated in
``docs/attention.md``; the short version is:

* opens -- the waiting set becomes non-empty;
* acknowledged -- a deliberate act (the notification is activated, the red badge is
  clicked, or a member task is opened). **Not** focus, and not merely landing on the
  Dashboard, which is the app's index route and so would be focus with extra steps;
* resets -- the waiting set empties;
* re-arms -- a task the previous reconcile did not see joins while the current episode
  is already acknowledged, which opens a new episode.

**Delivery is not the source of truth.** This module records only what happened to the
waiting set and whether a person acted on it. Whether any pixel reached a screen is the
client's business, and a client that was closed, denied permission, or muted by Windows
Focus Assist changes nothing here. That is what makes a restart -- of the browser or of
this server -- replay nothing: the episode is already open and already known.

**Where it lives.** ``~/.agentjobs/attention/<project>.yaml``, beside the dispatch
state and the finish directory rather than in the task store. It is machine state about
a person, not project history: it must not be exported with a task, must not travel in a
clone, and would be noise in a backup of the corpus. It *is* shared by every client this
server has -- the desktop and the phone read the same file -- which is what lets the
mobile child reuse this episode rather than start a second one.

Every transition below is computed inside the file's own merge lock, so two polls that
land in the same millisecond cannot each decide the episode is new.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .dashboard import human_waiting_tasks
from .dispatch.atomic_yaml import merge_yaml_atomically, read_yaml_resiliently
from .manager import TaskManager
from .models_v2 import Task
from .projects import default_home

ATTENTION_DIRNAME = "attention"
"""Directory under the AgentJobs home holding one document per project."""

SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
"""What may become a filename here.

Deliberately **not** :func:`agentjobs.projects.validate_project_id`, which is the
rule for an id a person may register and refuses a leading underscore. The reserved
id the server uses for a project resolved from the working directory is ``_local``,
so validating with that function turned every attention read on it into a 500 -- the
whole Playwright suite, which runs against exactly that project.

What this has to rule out is a path component that could leave the directory, which
is a separator, a drive letter or a traversal token. It is a guard on the filesystem
rather than a second opinion about what a project may be called.
"""


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_episode_id() -> str:
    """A fresh opaque episode id.

    Opaque and random rather than a counter, because a client stores the last id it
    notified for and compares it. A counter restarting at 1 -- which a deleted state
    file would do -- would match a stored id and silently suppress the notification the
    new episode exists to send.
    """
    return f"att_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Episode:
    """One run of attention: opened, possibly acknowledged, eventually reset.

    ``members`` is the waiting set **as of the last reconcile**, not everything that has
    ever waited. It is what "is this task new?" is asked against, so it has to shrink
    as well as grow: a task that closes and is handed back to the person weeks later is
    new attention and should re-arm, which it cannot do if it is remembered forever.
    """

    id: str
    started_at: datetime
    members: Tuple[str, ...]
    acknowledged_at: Optional[datetime] = None

    @property
    def acknowledged(self) -> bool:
        """Whether a person has deliberately acted on this episode."""
        return self.acknowledged_at is not None


@dataclass(frozen=True)
class AttentionState:
    """What the API answers with: the count, the waiting tasks, and the episode."""

    blocking: int
    waiting: Tuple[Task, ...]
    episode: Optional[Episode]

    @property
    def alerting(self) -> bool:
        """Whether an interruptive notification is owed for the current episode.

        True until somebody acknowledges it, however many clients have already drawn
        it. Each client dedupes its own delivery; this says only that the episode has
        not been acted on.
        """
        return self.episode is not None and not self.episode.acknowledged


def advance(
    previous: Optional[Episode],
    waiting: Sequence[str],
    *,
    now: Optional[datetime] = None,
    new_id: Callable[[], str] = _new_episode_id,
) -> Optional[Episode]:
    """The episode that follows *previous* given the current waiting set.

    Pure, total, and the only place the rule exists. ``None`` in and ``None`` out is
    the ordinary quiet state; ``None`` out with a non-empty *previous* is a reset.

    Idempotent in the sense the callers need: re-running it against an unchanged
    waiting set returns an equal episode, so the endpoint that reconciles on every poll
    does not manufacture attention by being polled.
    """
    moment = now or _now()
    current = tuple(waiting)

    if not current:
        # Reset. Everything cleared, so there is nothing to be told about and nothing
        # to remember -- the next wait is a new episode by construction.
        return None

    if previous is None:
        return Episode(id=new_id(), started_at=moment, members=current)

    if not previous.acknowledged:
        # Still unaddressed. New arrivals join it; the count moves and nothing
        # interrupts. This is the branch that makes a burst of handoffs one alert.
        return replace(previous, members=current)

    newcomers = [task_id for task_id in current if task_id not in previous.members]
    if newcomers:
        # Acknowledged, and then something new stopped. The person has dealt with the
        # last alert, so this one is allowed to interrupt again.
        return Episode(id=new_id(), started_at=moment, members=current)

    return replace(previous, members=current)


# ----- persistence -------------------------------------------------------------


def episode_path(project_id: str, *, home: Optional[Path] = None) -> Path:
    """The state document for one project.

    The id is checked here as well as wherever it came from, because this function turns
    it into a filename and a path component is the one place a string has to be trusted.
    """
    if not SAFE_PROJECT_ID.match(project_id):
        raise ValueError(f"project id {project_id!r} cannot be used as a filename")
    return (home or default_home()) / ATTENTION_DIRNAME / f"{project_id}.yaml"


def _to_document(episode: Optional[Episode]) -> Dict[str, Any]:
    """Render an episode as the YAML document, or the empty document for no episode."""
    if episode is None:
        return {}
    return {
        "episode_id": episode.id,
        "started_at": episode.started_at.isoformat(),
        "members": list(episode.members),
        "acknowledged_at": (
            episode.acknowledged_at.isoformat() if episode.acknowledged_at else None
        ),
    }


def _parse_moment(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_document(document: Any) -> Optional[Episode]:
    """Read an episode back, or ``None`` for anything that is not one.

    Deliberately forgiving. A missing file, an empty document, a half-edited one: all
    of them mean "no episode on record", and the worst that costs is one extra
    notification. Raising here would take the badge down with the state file.
    """
    if not isinstance(document, dict):
        return None
    episode_id = document.get("episode_id")
    started_at = _parse_moment(document.get("started_at"))
    if not isinstance(episode_id, str) or not episode_id or started_at is None:
        return None
    raw_members = document.get("members")
    members = tuple(str(m) for m in raw_members) if isinstance(raw_members, list) else ()
    return Episode(
        id=episode_id,
        started_at=started_at,
        members=members,
        acknowledged_at=_parse_moment(document.get("acknowledged_at")),
    )


def read_episode(project_id: str, *, home: Optional[Path] = None) -> Optional[Episode]:
    """The recorded episode without reconciling it against the corpus."""
    return _from_document(read_yaml_resiliently(episode_path(project_id, home=home)))


def _write_locked(
    path: Path, decide: Callable[[Optional[Episode]], Optional[Episode]]
) -> Optional[Episode]:
    """Apply *decide* to the stored episode as one read-modify-replace.

    ``merge_yaml_atomically`` holds a lock across the read and the write, which is what
    makes "several handoffs at once" a single episode rather than a race: the second
    poll to arrive reads the episode the first one opened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    decided: List[Optional[Episode]] = []

    def merge(current: Dict[str, Any], _fields: Dict[str, Any]) -> Dict[str, Any]:
        episode = decide(_from_document(current))
        decided.append(episode)
        return _to_document(episode)

    merge_yaml_atomically(path, {}, merge=merge)
    return decided[0] if decided else None


def reconcile(
    manager: TaskManager,
    project_id: str,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> AttentionState:
    """Read the waiting set, move the episode to match, and answer with both.

    Called by the endpoint the header already polls, so the episode advances whenever
    anything is watching and stays put when nothing is. That is the right dependency:
    the state is a fact about what a *person* has been told, and it is durable, so a
    poll that arrives an hour late still finds the episode that opened an hour ago
    rather than a fresh one.

    A write happens on every call, which sounds wasteful and is not: the document is
    under 200 bytes, the reconcile only runs on a poll the header was making anyway,
    and skipping the write when nothing changed would mean deciding what "changed"
    means in two places instead of one.
    """
    waiting = human_waiting_tasks(manager)
    ids = [task.id for task in waiting]
    episode = _write_locked(
        episode_path(project_id, home=home),
        lambda previous: advance(previous, ids, now=now),
    )
    return AttentionState(blocking=len(waiting), waiting=tuple(waiting), episode=episode)


def acknowledge(
    manager: TaskManager,
    project_id: str,
    episode_id: str,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> AttentionState:
    """Record that a person deliberately acted on *episode_id*, then reconcile.

    A stale id -- the episode reset or re-armed between the page's last poll and the
    click -- is **not** an error. It means the thing being acknowledged no longer
    exists, so there is nothing to record and the caller gets the current state back to
    render. Refusing would turn a race into an error message about a race.
    """
    moment = now or _now()

    def ack(previous: Optional[Episode]) -> Optional[Episode]:
        if previous is None or previous.id != episode_id or previous.acknowledged:
            return previous
        return replace(previous, acknowledged_at=moment)

    _write_locked(episode_path(project_id, home=home), ack)
    # Reconciled afterwards rather than in the same lock, so that a task which arrived
    # while the click was in flight opens its own episode here instead of being
    # silently acknowledged by a gesture aimed at the previous one.
    return reconcile(manager, project_id, home=home, now=now)
