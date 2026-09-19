"""The VAPID identity this machine pushes under (:rfc:`8292`, task-423).

One P-256 keypair per AgentJobs home, generated on first use and never afterwards. It
is the closest thing AgentJobs has to a server secret, and the two properties that
matter are both about where it is *not*:

* **Not in the repository, and not in a project.** ``~/.agentjobs/push/vapid.json``,
  beside the attention state and the dispatch state rather than inside a checkout. A
  clone of AgentJobs carries no key, a backup of the corpus carries no key, and a task
  export carries no key.
* **Not in the client.** The browser is handed the *public* half, which is what a
  subscription has to be created against, and the private half never leaves this
  process. That is the spec's "do not embed secrets in the client", and it is the whole
  reason the key is a keypair rather than a shared token.

**Rotating it invalidates every subscription**, because a browser binds its subscription
to the application-server key it was created with and a push signed by a different key
is refused. So the key is written once and read thereafter; the only supported rotation
is deleting the file, which :func:`load_or_create` treats as a first use and which
leaves the old subscriptions to be pruned by their own 403s.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

from ..projects import default_home
from .webpush import b64url_encode

PUSH_DIRNAME = "push"
"""Directory under the AgentJobs home holding the key and the per-project devices."""

VAPID_FILENAME = "vapid.json"

DEFAULT_CONTACT = "mailto:agentjobs@localhost"
"""The ``sub`` claim when nothing else is configured.

:rfc:`8292` wants a way to reach whoever is operating the application server, and every
major push service accepts a ``mailto:`` it will never use. AgentJobs runs on one
person's machine for that person, so there is nobody else to name; override it with
``AGENTJOBS_PUSH_CONTACT`` if a push service ever starts caring.
"""

TOKEN_LIFETIME_SECONDS = 12 * 3600
"""How long a signed assertion is good for. The RFC's ceiling is 24 hours; half of it
leaves room for a clock that is wrong in the direction that would otherwise fail."""


@dataclass(frozen=True)
class VapidKey:
    """The keypair, plus the two encodings the rest of the system asks for."""

    private_key: ec.EllipticCurvePrivateKey
    public_key_bytes: bytes

    @property
    def public_key(self) -> str:
        """base64url, unpadded -- what the browser passes as ``applicationServerKey``."""
        return b64url_encode(self.public_key_bytes)


def vapid_path(*, home: Optional[Path] = None) -> Path:
    """Where the keypair lives."""
    return (home or default_home()) / PUSH_DIRNAME / VAPID_FILENAME


def contact() -> str:
    """The ``sub`` claim, from the environment or the default."""
    return os.environ.get("AGENTJOBS_PUSH_CONTACT") or DEFAULT_CONTACT


def _restrict(path: Path) -> None:
    """Take the group and world bits off the key file where the platform has them.

    A no-op on Windows, where the ACL inherited from the profile directory is already
    the owner's alone, and worth doing anyway: the same home is mounted under WSL on
    this machine, and a key readable by every account is the kind of thing that is only
    ever noticed afterwards.
    """
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - filesystem without POSIX modes
        pass


def load_or_create(*, home: Optional[Path] = None) -> VapidKey:
    """The machine's VAPID keypair, generating and persisting one on first use.

    Called from the endpoint the client reads its ``applicationServerKey`` from, so the
    key comes into existence the first time somebody opens the notifications panel
    rather than at install time. There is no separate setup step and no key to copy.
    """
    path = vapid_path(home=home)
    existing = _read(path)
    if existing is not None:
        return existing

    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written with `x` so two processes racing to create the key cannot each decide
    # theirs is the one -- the loser re-reads the winner's rather than overwriting it,
    # which would strand every subscription created a moment earlier.
    document = json.dumps({"private_key_pem": pem})
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(document)
    except FileExistsError:
        winner = _read(path)
        if winner is not None:
            return winner
        # The file exists and is not a keypair. Replaced rather than raised on: a
        # server that will not start because a 200-byte state file got truncated is a
        # worse failure than losing subscriptions that were already unreachable --
        # every push under the lost key would have been refused anyway.
        path.write_text(document, encoding="utf-8")
    _restrict(path)
    return _key_from(private)


def _key_from(private: ec.EllipticCurvePrivateKey) -> VapidKey:
    return VapidKey(
        private_key=private,
        public_key_bytes=private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        ),
    )


def _read(path: Path) -> Optional[VapidKey]:
    """The stored keypair, or ``None`` for anything that is not one.

    Forgiving in the same way :mod:`agentjobs.attention` is forgiving about its state
    file, and for a different reason: a corrupt attention file costs one extra
    notification, while a corrupt key file costs every existing subscription. So this
    returns ``None`` -- meaning "generate a new one" -- only when there is genuinely
    nothing usable there, and a caller that wants the old key back should restore the
    file rather than hope.
    """
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        pem = document["private_key_pem"]
        private = load_pem_private_key(pem.encode("ascii"), password=None)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(private, ec.EllipticCurvePrivateKey):
        return None
    return _key_from(private)


def audience(endpoint: str) -> str:
    """The ``aud`` claim for an endpoint: its origin, and nothing after it.

    Scheme and host only. A token whose audience is the full endpoint is rejected by
    every push service, and the failure it produces -- a 401 on send, long after the
    subscription was accepted -- is the kind that gets diagnosed as "push does not
    work".
    """
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def authorization_header(key: VapidKey, endpoint: str, *, now: Optional[float] = None) -> str:
    """The ``Authorization: vapid t=…,k=…`` header for one endpoint.

    Signed per endpoint rather than once per round, because ``aud`` is the endpoint's
    origin and a person's devices are routinely on different push services -- an
    Android phone on FCM and an iPad on Apple's, from the same AgentJobs.
    """
    moment = int(now if now is not None else time.time())
    token = jwt.encode(
        {
            "aud": audience(endpoint),
            "exp": moment + TOKEN_LIFETIME_SECONDS,
            "sub": contact(),
        },
        key.private_key,
        algorithm="ES256",
    )
    return f"vapid t={token},k={key.public_key}"
