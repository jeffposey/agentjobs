"""Getting a run's environment to the worker that actually does the work (task-249).

``DispatchRunner._environment`` builds a correct environment and hands it to
``subprocess.run``. For a batch run that is the end of the story: the process it starts
is the process that works the task, and it inherits what it was given.

**A ``--bg`` session is not that.** The command AgentJobs runs is a *launcher*. It
contacts a persistent Claude Code daemon -- starting one only if none is up -- and the
daemon spawns the worker from **its own** environment. The launcher's environment is
discarded. So the two variables that say which run this is arrive only when the launch
happened to be the one that started the daemon, and a launch into an already-running
daemon inherits whatever the environment was when that daemon started, which may be
another run's identity from hours earlier.

Both halves have been observed on one machine within a day (task-249's log):

- ``run_68ea396e``, dispatched against task-316, came up holding ``run_12b2675c`` -- the
  task-269 supervisor, dispatched fourteen hours before. That cost it the merge
  authority a human had granted it, because ``finish.released_posture`` resolves the
  posture from the run the environment names.
- ``run_3f8ec46f``, dispatched two and a half minutes after the daemon idle-exited, came
  up correct, because its own launch started the daemon.

**The fix is to stop using the environment as the channel.** ``claude --help`` has no
per-session environment flag and ``--environment`` is an unrelated cloud-session id, but
``--settings`` takes "a settings JSON file or a JSON string ... to load additional
settings from", and a Claude Code settings document carries an ``env`` block. That
travels in **argv**, which the daemon does deliver -- it is how the prompt, the model and
the permission mode already arrive. Probed on Claude Code 2.1.238, 2026-08-25, against an
already-running daemon so the steady state was what was measured::

    SETTINGS=[settings-value-arrived] LAUNCHER=[]

**AgentJobs already uses that flag**, and this has to be got right rather than merely
noticed: ``posture_flags`` puts the run's *permission envelope* there -- the allow-list
and the enabled MCP servers -- as an inline JSON string. ``--settings`` is not
repeatable, so a second one would silently win and drop the first, which would mean
replacing the thing that decides what the run may do. The document is therefore **merged
into**, never appended beside; a document that cannot be read is left strictly alone.

**The merged document stays inline unless a runner declares its own ``env:``.** That
keeps the default case byte-identical in shape to what it has always been -- ``meta.yaml``
records argv verbatim, so a reader of a run record goes on seeing exactly which
permissions that run was granted, which is the point of recording argv at all. The
identity pair is not a secret: the run id is the name of the directory the record sits
in.

A runner's ``env:`` is the exception, because the design doc tells operators to put
secrets there *precisely because* argv is recorded. Inlining those would put them back
into the record, so a runner that declares any ``env:`` gets a file at mode ``0600``
beside the run, with only its path in argv -- and the merged document, with the ``env``
values redacted to their key names, is written to ``meta.yaml`` so the permission
envelope stays as auditable as it was before.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

SESSION_SETTINGS_FILENAME = "session-settings.json"
"""The per-run settings document, written into the run's own directory."""

SETTINGS_FLAG = "--settings"
"""The Claude Code flag that loads an additional settings document."""

DAEMON_START_BANNER = "starting background service"
"""What the launcher prints when *this* launch is the one that starts the daemon.

Matched case-insensitively and without its ellipsis. Nothing branches on the answer --
it is recorded so ``scripts/run_report.py`` can say whether a run's environment came
from its own launch or from a daemon somebody else started.
"""


class Delivery(str, Enum):
    """How a run's identity was got to its worker, recorded on ``meta.yaml``.

    The point of recording it is that "this run has no phase records" has three
    different causes and used to look identical in every report: the run predates the
    instrumentation, the instrumentation was there but its identity never arrived, or
    the identity arrived and no gate was ever run. Only the third is a fact about the
    work. A run with no ``session_env`` key at all is the first case.
    """

    DELIVERED = "delivered"
    """Merged into the ``--settings`` document argv carries."""

    CONFLICT = "conflict"
    """The runner's own argv carries a ``--settings`` this code could not read.

    Nothing is spliced. That flag holds the run's permission envelope, so replacing a
    document we cannot parse would be a worse failure than the one being fixed, and
    merging into one is guessing.
    """

    FAILED = "failed"
    """The settings file could not be written. The launch goes ahead without it.

    Only reachable for a runner with an ``env:`` of its own, which is the one case that
    needs a file rather than an inline document.
    """

    NOT_APPLICABLE = "not_applicable"
    """Not a driver that hops through a daemon, so the environment arrives directly."""


def session_environment(
    *,
    run_id: str,
    run_dir: Path,
    runner_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The variables a dispatched worker needs that its environment will not carry.

    The runner's own ``env:`` is included because it has the same problem and the same
    remedy: it is set on the launcher, and the launcher is not the worker.
    """
    environment: Dict[str, str] = dict(runner_env or {})
    # Ours last: a runner may not overwrite the identity of the run it is being started
    # for. The same precedence `_environment` applies, for the same reason.
    environment["AGENTJOBS_RUN_ID"] = run_id
    environment["AGENTJOBS_RUN_DIR"] = str(run_dir)
    return environment


def _existing_settings_index(argv: Sequence[str]) -> Optional[int]:
    """Where the runner's own ``--settings`` value sits in argv, if it has one."""
    for index, element in enumerate(argv):
        if element == SETTINGS_FLAG and index + 1 < len(argv):
            return index + 1
        if element.startswith(f"{SETTINGS_FLAG}="):
            return index
    return None


def _read_operator_settings(value: str) -> Optional[Dict[str, object]]:
    """The operator's own settings document, as a mapping, or None if unreadable.

    ``--settings`` accepts a JSON string or a path, so both are tried. None means "do
    not touch this argv" rather than "assume empty": an operator who configured settings
    we cannot parse still gets the settings they configured.
    """
    text = value
    candidate = Path(value)
    try:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def merged_document(
    environment: Mapping[str, str], base: Optional[Mapping[str, object]] = None
) -> Dict[str, object]:
    """One settings document: the operator's, with an ``env`` block merged into it.

    Their ``env`` keys survive except where they collide with the run's identity, which
    they may not win. Every other key of theirs -- ``permissions`` above all, which is
    what ``posture_flags`` puts here -- is carried through untouched.
    """
    document: Dict[str, object] = dict(base or {})
    merged: Dict[str, str] = {}
    existing = document.get("env")
    if isinstance(existing, Mapping):
        merged.update({str(key): str(value) for key, value in existing.items()})
    merged.update(environment)
    document["env"] = merged
    return document


def redacted(document: Mapping[str, object]) -> Dict[str, object]:
    """The document as it is safe to write into a run record: ``env`` values removed.

    The keys stay, because "which variables did this run get" is a question worth being
    able to answer from the record, and the values are the half that may be secret.
    """
    safe: Dict[str, object] = {}
    for key, value in document.items():
        if key == "env" and isinstance(value, Mapping):
            safe[key] = {str(name): "<redacted>" for name in value}
        else:
            safe[key] = value
    return safe


def write_session_settings(
    directory: Path,
    environment: Mapping[str, str],
    *,
    base: Optional[Mapping[str, object]] = None,
) -> Path:
    """Write the settings document for one run and return its path."""
    document = merged_document(environment, base)
    path = directory / SESSION_SETTINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    try:
        # A runner's `env:` is where the design doc tells operators to put secrets. A
        # best-effort narrowing on POSIX, and close to a no-op on Windows where the
        # containing home is already user-scoped.
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform-dependent and never worth failing on
        pass
    return path


@dataclass(frozen=True)
class Delivered:
    """What a delivery did, for the run record to keep."""

    argv: List[str]
    delivery: Delivery
    document: Optional[Dict[str, object]] = None
    """The merged settings, ``env`` redacted -- or None when nothing was spliced.

    Recorded on ``meta.yaml`` only when the document went to a file. Inline, argv already
    holds it verbatim and a second copy would be one more thing that can disagree.
    """


def deliver_identity(
    argv: Sequence[str],
    *,
    prompt: str,
    directory: Path,
    run_id: str,
    runner_env: Optional[Mapping[str, str]] = None,
) -> Delivered:
    """Get this run's identity into argv, merging rather than displacing what is there.

    **It never raises.** A run that cannot be measured is a gap in a report; a run that
    dies because it could not be measured is a lost hour of work. That is the same trade
    ``phases.record_phase`` makes, and it is why every failure below returns a verdict
    instead of an exception.

    Two shapes, and which one is used turns only on whether the runner declared an
    ``env:`` of its own:

    - **Inline**, the default. The merged document goes back into argv as JSON, exactly
      where ``posture_flags`` already puts one. ``meta.yaml`` records argv verbatim, so
      the permission envelope stays as readable in the run record as it has always been.
    - **A file**, when the runner has an ``env:``. Those values are where operators are
      told to put secrets *because* argv is recorded, so they may not go back into it.
      Only the path is spliced, and the redacted document is returned for the record.

    The flag is spliced immediately before the element carrying the prompt when there is
    no existing one -- where ``compose_argv`` puts the posture flags, because a CLI
    expects options before a positional argument. Doing this before ``_plan_wake`` also
    means ``wake_argv`` carries it through to a resumed session untouched: that rewrites
    only the element holding the prompt.
    """
    rendered = list(argv)
    base: Optional[Mapping[str, object]] = None

    at = _existing_settings_index(rendered)
    if at is not None:
        raw = rendered[at]
        inline_form = raw.startswith(f"{SETTINGS_FLAG}=")
        if inline_form:
            raw = raw.split("=", 1)[1]
        base = _read_operator_settings(raw)
        if base is None:
            # Never clobber. Replacing settings we cannot read -- the permission envelope
            # among them -- would be a worse failure than the one being fixed.
            return Delivered(rendered, Delivery.CONFLICT)
    else:
        inline_form = False

    environment = session_environment(run_id=run_id, run_dir=directory, runner_env=runner_env)

    if runner_env:
        try:
            path = write_session_settings(directory, environment, base=base)
        except OSError:
            return Delivered(rendered, Delivery.FAILED)
        value: str = str(path)
        document = redacted(merged_document(environment, base))
    else:
        value = json.dumps(merged_document(environment, base))
        document = None

    if at is not None:
        rendered[at] = f"{SETTINGS_FLAG}={value}" if inline_form else value
        return Delivered(rendered, Delivery.DELIVERED, document)

    insert_at = len(rendered)
    if prompt:
        for index, element in enumerate(rendered):
            if prompt in element:
                insert_at = index
                break
    return Delivered(
        [*rendered[:insert_at], SETTINGS_FLAG, value, *rendered[insert_at:]],
        Delivery.DELIVERED,
        document,
    )


def daemon_was_started(launcher_output: str) -> bool:
    """Whether this launch started the daemon, read off the launcher's own banner.

    Recorded on the run so a report can distinguish a run whose environment came from
    its own launch from one that inherited a daemon started by something else. Only 12
    of 61 runs on this machine printed it, which is what made the defect above steady
    state rather than an edge case.
    """
    return DAEMON_START_BANNER in launcher_output.lower()
