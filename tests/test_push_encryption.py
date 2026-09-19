"""RFC 8291 and RFC 8292, checked against the specifications rather than against us.

The argument for implementing Web Push here instead of taking ``pywebpush`` is that
what is left once ``cryptography`` is present is small and **verifiable**. This file is
the second half of that argument: without a known-answer test, a hand-rolled encryption
is only asserted to agree with itself, and this one is driven by the worked example
:rfc:`8291` section 5 publishes -- every key, the salt and the expected body fixed --
so a derivation that is subtly wrong cannot pass.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agentjobs.push import keys as push_keys
from agentjobs.push.webpush import (
    MAX_PAYLOAD,
    b64url_decode,
    b64url_encode,
    decrypt,
    encrypt,
)

# ----- RFC 8291, section 5 ----------------------------------------------------------

RFC_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def _private_key(raw: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(raw), "big"), ec.SECP256R1())


def test_matches_the_rfc_8291_worked_example() -> None:
    """Byte for byte, including the header the receiver parses the key out of."""
    result = encrypt(
        RFC_PLAINTEXT,
        receiver_public_key=b64url_decode(RFC_UA_PUBLIC),
        auth_secret=b64url_decode(RFC_AUTH),
        salt=b64url_decode(RFC_SALT),
        server_private_key=_private_key(RFC_AS_PRIVATE),
    )
    assert b64url_encode(result.body) == RFC_BODY


def test_a_browser_can_read_what_we_send() -> None:
    """The other direction, with the RFC's own receiving key."""
    result = encrypt(
        RFC_PLAINTEXT,
        receiver_public_key=b64url_decode(RFC_UA_PUBLIC),
        auth_secret=b64url_decode(RFC_AUTH),
        salt=b64url_decode(RFC_SALT),
        server_private_key=_private_key(RFC_AS_PRIVATE),
    )
    assert (
        decrypt(
            result.body,
            receiver_private_key=_private_key(RFC_UA_PRIVATE),
            auth_secret=b64url_decode(RFC_AUTH),
        )
        == RFC_PLAINTEXT
    )


def test_every_message_uses_a_fresh_ephemeral_key_and_salt() -> None:
    """Two pushes of the same text to one device must not look alike on the wire.

    A fixed ephemeral key would make the ciphertexts identical, which tells whichever
    push service is carrying them that nothing has changed -- and, worse, reuses a
    nonce under one content key.
    """
    args: Dict[str, Any] = {
        "receiver_public_key": b64url_decode(RFC_UA_PUBLIC),
        "auth_secret": b64url_decode(RFC_AUTH),
    }
    first = encrypt(b"same text", **args)
    second = encrypt(b"same text", **args)
    assert first.body != second.body
    assert first.salt != second.salt
    assert first.server_public_key != second.server_public_key


def test_a_real_browser_round_trip() -> None:
    """A freshly generated receiver, which is what a subscription actually is."""
    receiver = ec.generate_private_key(ec.SECP256R1())
    public = b64url_encode(
        receiver.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    auth = b"0123456789abcdef"
    payload = json.dumps({"kind": "attention", "blocking": 3}).encode()
    body = encrypt(payload, receiver_public_key=b64url_decode(public), auth_secret=auth).body
    assert decrypt(body, receiver_private_key=receiver, auth_secret=auth) == payload


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"receiver_public_key": b"\x04" + b"\x00" * 10}, "uncompressed P-256 point"),
        ({"auth_secret": b"short"}, "16 bytes"),
    ],
)
def test_malformed_subscription_material_is_refused(kwargs: dict, message: str) -> None:
    """Refused here rather than sent, so the failure names the row and not the service."""
    base: Dict[str, Any] = {
        "receiver_public_key": b64url_decode(RFC_UA_PUBLIC),
        "auth_secret": b64url_decode(RFC_AUTH),
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        encrypt(b"x", **base)


def test_a_payload_too_large_for_one_record_is_refused() -> None:
    """Not split. A multi-record body is a feature nothing here needs."""
    with pytest.raises(ValueError, match="one record holds"):
        encrypt(
            b"x" * (MAX_PAYLOAD + 1),
            receiver_public_key=b64url_decode(RFC_UA_PUBLIC),
            auth_secret=b64url_decode(RFC_AUTH),
        )


def test_padded_and_unpadded_base64url_both_decode() -> None:
    """Browsers omit the padding; some clients restore it. Both are the same bytes."""
    assert b64url_decode("BTBZMqHH6r4Tts7J_aSIgg") == b64url_decode("BTBZMqHH6r4Tts7J_aSIgg==")


# ----- RFC 8292 ---------------------------------------------------------------------


def test_vapid_header_verifies_against_the_published_key(tmp_path) -> None:
    """The assertion a push service checks: right claims, right signature, right key.

    Verified with PyJWT's own decoder against the *public* half, which is the same
    operation the push service performs -- so this fails if the token is signed with
    the wrong key, carries the endpoint instead of its origin, or is not ES256.
    """
    key = push_keys.load_or_create(home=tmp_path)
    endpoint = "https://fcm.googleapis.com/fcm/send/abc123?x=1"
    header = push_keys.authorization_header(key, endpoint)

    assert header.startswith("vapid t=")
    token, advertised = header[len("vapid ") :].split(",")
    assert advertised == f"k={key.public_key}"

    claims = jwt.decode(
        token[len("t=") :],
        key.private_key.public_key(),
        algorithms=["ES256"],
        audience="https://fcm.googleapis.com",
    )
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == push_keys.DEFAULT_CONTACT
    assert 0 < claims["exp"] - time.time() <= push_keys.TOKEN_LIFETIME_SECONDS


def test_the_audience_is_the_origin_and_not_the_endpoint() -> None:
    """A token whose audience is the full URL is refused by every push service."""
    assert (
        push_keys.audience("https://updates.push.services.mozilla.com/wpush/v2/gAAAA?x=1")
        == "https://updates.push.services.mozilla.com"
    )


def test_the_keypair_is_generated_once_and_reused(tmp_path) -> None:
    """Rotating it would invalidate every subscription, so it is written once."""
    first = push_keys.load_or_create(home=tmp_path)
    second = push_keys.load_or_create(home=tmp_path)
    assert first.public_key == second.public_key
    assert push_keys.vapid_path(home=tmp_path).exists()


def test_a_corrupt_key_file_is_replaced_rather_than_raising(tmp_path) -> None:
    """The cost is every existing subscription, which is why it is only done when the
    file is genuinely unusable -- but a server that will not start is worse."""
    path = push_keys.vapid_path(home=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")
    assert push_keys.load_or_create(home=tmp_path).public_key


def test_the_private_key_never_appears_in_the_public_half(tmp_path) -> None:
    """The client is handed `public_key` and nothing else; this is that claim, asserted."""
    key = push_keys.load_or_create(home=tmp_path)
    stored = push_keys.vapid_path(home=tmp_path).read_text(encoding="utf-8")
    assert "PRIVATE KEY" in stored
    assert key.public_key not in stored
    assert len(b64url_decode(key.public_key)) == 65


def test_the_contact_can_be_overridden(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENTJOBS_PUSH_CONTACT", "mailto:someone@example.com")
    key = push_keys.load_or_create(home=tmp_path)
    header = push_keys.authorization_header(key, "https://example.com/push/1")
    token = header[len("vapid t=") :].split(",")[0]
    claims = jwt.decode(
        token,
        key.private_key.public_key(),
        algorithms=["ES256"],
        audience="https://example.com",
    )
    assert claims["sub"] == "mailto:someone@example.com"
