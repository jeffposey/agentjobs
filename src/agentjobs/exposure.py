"""Which principals a project is served to, and what a hidden one answers.

Task-332 answered *what may this caller do*. This module answers the question that one
deliberately left alone: **what may this caller read**. They are different questions and
they need different machinery, because a capability is a property of the caller and
exposure is a property of the **data**.

That distinction is the whole of this module's reason to exist. Two projects on this
machine carry a standing "local only, never push" rule, and knowing who is asking does
not by itself keep them local: ``/api/all/tasks`` returns every project's tasks to
whoever asks, and ``/runs/{id}/transcript`` serves a file AgentJobs does not own, whose
contents are whatever that session happened to look at. A run that read a local-only
project has that project's material in its transcript, under a route about *runs*. So
the check cannot live on the routes that mention those projects by name -- there is no
such route -- it has to live on the project, and every surface has to ask.

**The setting is ``visibility`` in the project's own ``.agentjobs/config.yaml``.**

.. code-block:: yaml

    visibility: local     # or: shared

``shared`` (the default) means every principal may read it. ``local`` means only the
principals that are already on this machine. It lives in the *project's* config rather
than in the machine registry because it is a property of the project -- ``vault`` is
local-only wherever it is checked out, and a setting that travels with the repository
says so once instead of once per machine.

**Why ``visibility`` and not ``expose_on_proxy``.** The audit that found this proposed
the latter, and it names the wrong thing: the control is about which *principals* may
read the project, and the proxy is merely today's only way for a non-local one to
arrive. A second front door -- a second proxy, a LAN bind, an authenticated public
endpoint -- would make a proxy-shaped name read as a lie while the code kept working,
which is the worst kind of wrong name. ``visibility`` is a property of the thing being
read, which is what this is.

**Local means "on this machine", so a ``run`` is not excluded.** A dispatched agent runs
here, as a local process, with the project's files on its filesystem; refusing it over
HTTP would stop nothing and would break a run dispatched *into* a local-only project
from reading its own task. ``tailnet`` is the kind this excludes, because it is the one
whose identity was established from somewhere else. That is a rule about where the
caller is, not about how much we trust it -- the two human kinds remain equally
privileged (see :mod:`agentjobs.capabilities`), and a ``tailnet`` caller retains every
capability it had over the projects it can still see.

**Absent means absent.** A caller who cannot see a project is told the project does not
exist, never that it may not read it -- the convention ``_owned_run`` already follows in
``api/routes/dispatch.py``, and for the same reason: a refusal that distinguishes
"forbidden" from "not here" tells a remote caller that the hidden thing exists, which is
most of what hiding it was for. This module supplies the predicate; the 404 is the
caller's, because only the caller knows what shape its "absent" is.

Nothing here raises, knows about HTTP, or reads a file. It takes a config mapping and a
principal and answers a boolean -- the same split :mod:`agentjobs.principals` and
:mod:`agentjobs.capabilities` make, so the rule stays testable without a transport.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional

from .principals import Principal, PrincipalKind

VISIBILITY_KEY = "visibility"
"""The project-config key. One word, top level, beside ``project_name``."""


class Visibility(str, Enum):
    """Who a project is served to. Two values, and a third would need a new kind."""

    SHARED = "shared"
    """Every principal may read it. The default, and what every project was before."""

    LOCAL = "local"
    """Only principals already on this machine. A remote caller is told it is not here."""


DEFAULT_VISIBILITY = Visibility.SHARED
"""What a project that has never heard of this setting gets.

**Open, and that is the deliberate half of this design.** Defaulting closed would be the
safer-sounding choice and the wrong one: every existing install would lose its phone at
upgrade, silently and with no error naming the cause, and a change with that shape gets
reverted rather than configured. The line this task holds is that the exposed projects
keep working from a phone -- it removes hidden projects from remote view, it does not
remove a feature from the ones that remain.

What makes that acceptable rather than merely convenient is that this is a *disclosure*
control in front of a door that is already authenticated: reaching a ``tailnet``
principal at all means the front door proved an identity. Default-open here decides
which of that person's own projects their phone can see, not whether a stranger can see
any. The safety is carried by :func:`visibility_of` instead, which fails closed on
anything it cannot read -- a project that *says* something about its visibility and says
it wrongly is hidden, so the failure mode of a typo is a missing project rather than a
published one.

New projects do not rely on the default: ``project_setup.DEFAULT_CONFIG`` writes the key
explicitly, so a project created today records a decision rather than inheriting one.
"""

LOCAL_KINDS: FrozenSet[PrincipalKind] = frozenset(
    {
        PrincipalKind.OWNER,
        PrincipalKind.RUN,
    }
)
"""The principal kinds that are already on this machine.

Written as membership rather than as ``!= TAILNET`` so that a fourth kind forces a
decision here instead of silently inheriting the answer for local ones. Both members
reach the application over loopback and both can read the project's files directly, which
is the property that makes withholding them over HTTP pointless rather than merely
unnecessary.
"""


def visibility_of(config: Optional[Mapping[str, Any]]) -> Visibility:
    """Read a project's visibility from its config, failing closed on anything odd.

    An absent key is :data:`DEFAULT_VISIBILITY`, which is ``shared`` -- see there for why
    the default is the open one. **A key that is present and unreadable is ``local``**,
    which is the opposite default and is the point: the only way to get an unrecognised
    value is to have written one, so somebody was trying to say something about this
    project's exposure and mistyped it. Hiding it is recoverable in one edit; publishing
    it is not.
    """
    if not config:
        return DEFAULT_VISIBILITY
    raw = config.get(VISIBILITY_KEY)
    if raw is None:
        return DEFAULT_VISIBILITY
    if isinstance(raw, str):
        try:
            return Visibility(raw.strip().lower())
        except ValueError:
            return Visibility.LOCAL
    return Visibility.LOCAL


def readable_by(visibility: Visibility, principal: Optional[Principal]) -> bool:
    """True when a project of this visibility is served to this principal.

    ``principal`` is ``None`` for a request that resolved to nobody -- a caller from an
    address the front door does not control. That is refused for a ``local`` project: a
    caller we could not identify is certainly not one we established to be on this
    machine, and "we could not tell" must never widen what is served.
    """
    if visibility is Visibility.SHARED:
        return True
    if principal is None:
        return False
    return principal.kind in LOCAL_KINDS


def hidden_from(visibility: Visibility, principal: Optional[Principal]) -> bool:
    """The negation, spelled out, for call sites that read better as a refusal."""
    return not readable_by(visibility, principal)


__all__ = [
    "DEFAULT_VISIBILITY",
    "LOCAL_KINDS",
    "VISIBILITY_KEY",
    "Visibility",
    "hidden_from",
    "readable_by",
    "visibility_of",
]
