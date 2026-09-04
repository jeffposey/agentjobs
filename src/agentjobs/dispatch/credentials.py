"""Minting the credential a dispatched run identifies itself with (task-331).

Task-329 built the seam: :mod:`agentjobs.principals` resolves one principal per
request and checks a run credential *first*, because a dispatched agent arrives on
loopback exactly as the person at the keyboard does. It shipped with a verifier that
verifies nothing, so no request could resolve ``run``. This module is the other half --
it mints the credential at dispatch and verifies it at the API -- and with it installed
loopback finally splits in two: **without** a credential you are the owner, **with** one
you are that specific run.

That split is the whole point. Auditor 10's P1-2 finding is that an agent inside a run
can ``POST .../dispatch`` naming a human and be believed, and no proxy change can reach
it: a dispatched agent never goes through the proxy.

**What this buys, stated plainly, because a later reader will otherwise assume more.**
A run can read its own credential out of its own environment. Nothing stops it passing
that credential to something else, and nothing here pretends otherwise. The property is
**"an agent cannot claim to be a person"**, not "an agent cannot misbehave as itself". A
run principal is strictly less capable than an owner -- once task-332 gives either of
them capabilities -- so there is no escalation path through it; but it is not a secret
from the agent holding it, and anything built on the assumption that it is will be
unsound.

**Where the secret lives, and where it does not.** The token is ``<run_id>.<nonce>``.
Only its SHA-256 digest is written to disk, in the run's own directory, so the ledger
holds nothing that can be replayed. The token itself exists in exactly two places: the
child process's environment, and -- for a Claude ``--bg`` session, whose worker is
spawned by a daemon that discards the launcher's environment -- the ``0600``
session-settings file that ``session_env`` already writes for exactly this class of
value. It is deliberately kept out of argv, because argv is recorded verbatim into
``meta.yaml`` and into the task's dispatch log entry.

**Expiry is the run's own status**, read at verification time rather than a timestamp
baked into the token. A finished, cancelled or failed run's credential is refused, and
refused *as a refusal*: it never falls through to ``owner``. Falling back on expiry
would invert the control -- presenting a dead run's credential would be a way to be
promoted from agent to person.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Mapping, Optional, Union

from agentjobs.principals import Problem, Refusal, RunCredential

CREDENTIAL_ENV = "AGENTJOBS_RUN_CREDENTIAL"
"""The environment variable a dispatched worker finds its credential in.

Named beside ``AGENTJOBS_RUN_ID`` and ``AGENTJOBS_RUN_DIR`` because it travels with
them, and separately from them because it is the only one of the three that is a secret.
"""

CREDENTIAL_FILENAME = "credential.sha256"
"""Where a run's credential digest is kept, inside the run's own directory.

The digest, never the token. A ledger that stored the token would be a directory full of
replayable credentials for every run this machine has ever had.
"""

_RUN_ID = re.compile(r"\Arun_[0-9a-f]{8,}\Z")
"""What a run id may look like, checked before it is used as a path segment.

``new_run_id`` produces ``run_`` and eight hex characters. This pattern is applied to a
value that arrived in a **request header**, so it is a path-traversal guard first and a
shape check second: without it, a presented credential could name a parent directory and
ask this module to read a digest from anywhere on the disk.
"""


def _digest(token: str) -> str:
    """The stored form of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digest_path(directory: Path) -> Path:
    """Where the digest for the run in *directory* lives."""
    return directory / CREDENTIAL_FILENAME


def mint_run_credential(directory: Path, run_id: str) -> str:
    """Mint this run's credential, store its digest, and return the token.

    Called once per run, immediately before the worker is started, so the digest is on
    disk before anything can present the token.

    **It never raises**, and returns ``""`` when the digest cannot be written. That is
    the same trade ``session_env.deliver_identity`` and ``phases.record_phase`` make: a
    run that cannot be identified is a gap in an audit trail, and a run that dies because
    it could not be identified is a lost hour of work. A run with no credential is
    indistinguishable from one dispatched before this existed, which is a case that has
    to keep working anyway.

    ``secrets.token_urlsafe(32)`` is 256 bits from the OS CSPRNG. The run id is carried
    in the clear in front of it so verification is a single directory read rather than a
    scan of every run the machine has ever had; the run id is not a secret -- it is the
    name of the directory the record sits in, and it is already in argv.
    """
    if not _RUN_ID.match(run_id):
        return ""
    token = f"{run_id}.{secrets.token_urlsafe(32)}"
    path = digest_path(directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_digest(token), encoding="utf-8")
    except OSError:
        return ""
    try:
        # Best effort on POSIX, close to a no-op on Windows where the containing home is
        # already user-scoped. The digest is not the token, so this is defence in depth
        # rather than the thing keeping the secret.
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform-dependent and never worth failing on
        pass
    return token


def revoke_run_credential(directory: Path) -> None:
    """Delete a run's digest, so its credential can never verify again.

    Not what enforces expiry -- :func:`verify_run_credential` reads the run's status for
    that, and refuses even a directory whose digest survived. This is the belt to that
    pair of braces, for the ordinary case where a concluded run's directory stays on disk
    for months.
    """
    try:
        digest_path(directory).unlink()
    except OSError:
        pass


def presented_credential(environ: Optional[Mapping[str, str]] = None) -> str:
    """This process's own credential, if it is running inside a dispatched run."""
    source = environ if environ is not None else os.environ
    return (source.get(CREDENTIAL_ENV) or "").strip()


def verify_run_credential(
    presented: str, home: Optional[Path] = None
) -> Union[RunCredential, Refusal, None]:
    """Turn a presented token into the run it proves, or say why it proves nothing.

    Installed as :mod:`agentjobs.principals`' verifier by the API application. The three
    answers are deliberately distinct:

    - a :class:`~agentjobs.principals.RunCredential`, naming the run and its task;
    - a :class:`~agentjobs.principals.Refusal` carrying
      :attr:`~agentjobs.principals.Problem.EXPIRED_RUN_CREDENTIAL`, when the token is
      genuine and its run is over;
    - ``None``, when the token proves nothing at all -- malformed, unknown run, or a
      digest that does not match.

    ``None`` and a refusal both stop resolution dead, and neither falls through to
    ``owner``. They are told apart because "somebody presented a forgery" and "a real run
    outlived its credential" are different events for an operator reading a log, and
    because task-332 may well want to answer them differently.

    The comparison is ``secrets.compare_digest`` rather than ``==``: the values compared
    are hashes, so a timing oracle on them is of little use, but the cost of doing it
    right is one function call.

    The ledger is imported late so this module stays free of ``dispatch.runner``, which
    imports ``session_env``, which is where minting is wired in.
    """
    from agentjobs.dispatch.ledger import read_run
    from agentjobs.dispatch.runner import runs_root

    token = (presented or "").strip()
    run_id, _, nonce = token.partition(".")
    if not nonce or not _RUN_ID.match(run_id):
        return None

    root = home if home is not None else _default_home()
    if root is None:
        return None
    directory = runs_root(root) / run_id
    if not directory.is_dir():
        return None
    try:
        stored = digest_path(directory).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not stored or not secrets.compare_digest(stored, _digest(token)):
        return None

    record = read_run(directory)
    if not record.is_live:
        return Refusal(
            problem=Problem.EXPIRED_RUN_CREDENTIAL,
            detail=(
                f"The credential for {run_id} verifies, but that run is "
                f"{record.outcome or record.status}. A credential dies with its run, and "
                "an expired one is refused rather than downgraded to the owner: falling "
                "back to a *more* capable principal on expiry would invert the control."
            ),
        )
    return RunCredential(run_id=record.run_id or run_id, task_id=record.task_id)


def _default_home() -> Optional[Path]:
    """The AgentJobs home whose runs are consulted, or None if it cannot be found."""
    from agentjobs.projects import default_home

    try:
        return default_home()
    except OSError:  # pragma: no cover - only a home that cannot be resolved at all
        return None
