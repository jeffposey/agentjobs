"""Web Push, from the two RFCs and nothing else (task-423).

**Why there is no push library here.** The transport the spec asked for is the smallest
one that reaches an installable web client on both mobile platforms, and that is
standards-based Web Push: RFC 8030 for the protocol, :rfc:`8291` for the payload
encryption, :rfc:`8292` for the application-server identity. The usual Python route is
``pywebpush``, which brings ``http-ece``, ``py-vapid`` and ``cryptography``. AgentJobs
already has ``cryptography`` and ``PyJWT`` in its closure, and what is left over once
those two are present is the hundred lines below -- an ECDH, two HKDF extracts, three
expands and an AES-GCM. So the dependency would buy packaging, not cryptography.

That trade is only defensible because the result is checkable rather than plausible:
:rfc:`8291` section 5 publishes a complete worked example with every intermediate value
fixed, and ``tests/test_push_encryption.py`` drives this module with those exact inputs
and asserts that exact body. A hand-rolled encryption with no known-answer test would be
the wrong call whatever it saved.

**What is encrypted and what is not.** The body of a push is encrypted end to end: the
push service -- Google's, Apple's, Mozilla's -- routes it and cannot read it. What the
service does see is the endpoint, the size, and the timing. That is why the payload is
a wake-up signal by design rather than merely by convention, and why
:mod:`agentjobs.push.delivery` keeps task titles out of it unless the owner asks for
them.
"""

from __future__ import annotations

import base64
import hmac
import os
import struct
from dataclasses import dataclass
from hashlib import sha256
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

RECORD_SIZE = 4096
"""The single-record size advertised in the header.

AgentJobs never sends more than one record, so this is a ceiling rather than a chunk
size: it says how large a record the receiver must be prepared for. A payload that does
not fit in one is refused by :func:`encrypt` rather than silently split, because a
multi-record body is a feature nothing here needs and every browser would rather not
receive.
"""

MAX_PAYLOAD = RECORD_SIZE - 16 - 1
"""What fits in one record: the record size less the GCM tag and the padding delimiter."""

_KEY_INFO = b"WebPush: info\x00"
_CEK_INFO = b"Content-Encoding: aes128gcm\x00"
_NONCE_INFO = b"Content-Encoding: nonce\x00"


def b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url, the encoding every field of a subscription uses.

    Browsers omit the padding and :func:`base64.urlsafe_b64decode` insists on it, so the
    padding is restored here. Tolerating the padded form too costs one expression and
    saves a class of report that reads as "push is broken" and is a client that padded.
    """
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def b64url_encode(value: bytes) -> str:
    """Encode as unpadded base64url, which is what both RFCs put on the wire."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (:rfc:`5869`), which is one HMAC and has no wrapper in ``cryptography``.

    ``HKDF`` there does extract-and-expand in one object, and Web Push needs the two
    halves separately: the first PRK is expanded with a key agreement in the info, and
    the second is extracted with a salt that travels in the header.
    """
    return hmac.new(salt, ikm, sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand with the pseudo-random key already in hand."""
    return HKDFExpand(algorithm=SHA256(), length=length, info=info).derive(prk)


@dataclass(frozen=True)
class EncryptedPush:
    """A finished ``aes128gcm`` body, ready to POST at a subscription's endpoint."""

    body: bytes
    salt: bytes
    server_public_key: bytes


def encrypt(
    plaintext: bytes,
    *,
    receiver_public_key: bytes,
    auth_secret: bytes,
    salt: Optional[bytes] = None,
    server_private_key: Optional[ec.EllipticCurvePrivateKey] = None,
) -> EncryptedPush:
    """Encrypt *plaintext* for one subscription, per :rfc:`8291`.

    *receiver_public_key* is the subscription's ``p256dh`` as bytes -- the uncompressed
    P-256 point, 65 bytes beginning ``0x04`` -- and *auth_secret* is its ``auth``, 16
    bytes. Both arrive base64url-encoded from the browser; decode them with
    :func:`b64url_decode` first.

    *salt* and *server_private_key* exist for the known-answer test and for nothing
    else. Left unset they are freshly random per message, which is what makes every
    push a distinct ciphertext under a distinct ephemeral key -- the property that lets
    the same plaintext be sent to five devices without any two of them looking alike to
    the push services carrying them.
    """
    if len(plaintext) > MAX_PAYLOAD:
        raise ValueError(f"push payload is {len(plaintext)} bytes; one record holds {MAX_PAYLOAD}")
    if len(receiver_public_key) != 65 or receiver_public_key[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point of 65 bytes")
    if len(auth_secret) != 16:
        raise ValueError("auth secret must be 16 bytes")

    salt = salt if salt is not None else os.urandom(16)
    private = server_private_key or ec.generate_private_key(ec.SECP256R1())
    server_public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_public_key)

    shared = private.exchange(ec.ECDH(), receiver)
    # The auth secret is the salt of the *first* extract, which is what binds the
    # derived key to this subscription rather than only to the key agreement. Without
    # it, anybody who learned the endpoint and the public key could encrypt.
    ikm = _hkdf_expand(
        _hkdf_extract(auth_secret, shared),
        _KEY_INFO + receiver_public_key + server_public,
        32,
    )
    prk = _hkdf_extract(salt, ikm)
    content_key = _hkdf_expand(prk, _CEK_INFO, 16)
    nonce = _hkdf_expand(prk, _NONCE_INFO, 12)

    # 0x02 rather than 0x01: this is the last record, and a receiver that reads 0x01
    # here waits for a continuation that is never coming.
    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!L", RECORD_SIZE) + bytes([len(server_public)]) + server_public
    return EncryptedPush(body=header + ciphertext, salt=salt, server_public_key=server_public)


def decrypt(
    body: bytes, *, receiver_private_key: ec.EllipticCurvePrivateKey, auth_secret: bytes
) -> bytes:
    """Undo :func:`encrypt`, as a browser would.

    Here so the tests can assert what a device receives rather than only what the
    server sends. Nothing in the running application calls it -- AgentJobs is the
    application server, and an application server never decrypts a push -- but a
    round-trip test that never decrypts is asserting that the encryptor agrees with
    itself.
    """
    salt = body[:16]
    key_length = body[20]
    server_public = body[21 : 21 + key_length]
    ciphertext = body[21 + key_length :]

    receiver_public = receiver_private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    shared = receiver_private_key.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_public)
    )
    ikm = _hkdf_expand(
        _hkdf_extract(auth_secret, shared),
        _KEY_INFO + receiver_public + server_public,
        32,
    )
    prk = _hkdf_extract(salt, ikm)
    plaintext = AESGCM(_hkdf_expand(prk, _CEK_INFO, 16)).decrypt(
        _hkdf_expand(prk, _NONCE_INFO, 12), ciphertext, None
    )
    # Trailing zeros first, then the one delimiter byte. That order and not the other:
    # the padding of a record is the delimiter followed by the zeros, so stripping the
    # delimiter first finds zeros where the delimiter should have been.
    unpadded = plaintext.rstrip(b"\x00")
    return unpadded[:-1] if unpadded[-1:] in (b"\x01", b"\x02") else unpadded
