import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from apps.notifications.services.webpush import (
    MAX_PAYLOAD,
    BrowserKeys,
    VapidKeys,
    b64url,
    public_key,
    unb64url,
)
from apps.notifications.tests.browsers import Browser

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
# The receiver of RFC 8291 appendix A.
RECEIVER = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"


def test_the_body_matches_the_example_of_rfc_8291() -> None:
    """RFC 8291 appendix A: the same keys and salt give the same bytes."""
    keys = BrowserKeys(RECEIVER, "BTBZMqHH6r4Tts7J_aSIgg")
    sender = VapidKeys.load("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw").private_key

    body = keys.encrypt(
        b"When I grow up, I want to be a watermelon",
        salt=unb64url("DGv6ra1nlYgDCS1FRnbzlw"),
        sender=sender,
    )

    assert b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLoc"
        "InmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLV"
        "WGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_the_browser_reads_what_was_encrypted_for_it() -> None:
    browser = Browser()

    body = BrowserKeys(browser.p256dh, browser.auth).encrypt("Привет".encode())

    assert browser.decrypt(body) == "Привет".encode()


def test_each_message_has_its_own_salt_and_key() -> None:
    browser = Browser()
    keys = BrowserKeys(browser.p256dh, browser.auth)

    assert keys.encrypt(b"same") != keys.encrypt(b"same")


def test_a_payload_over_the_limit_is_refused() -> None:
    browser = Browser()

    with pytest.raises(ValueError, match="too long"):
        BrowserKeys(browser.p256dh, browser.auth).encrypt(b"x" * (MAX_PAYLOAD + 1))


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [("not-a-key", b64url(b"0" * 16)), (RECEIVER, b64url(b"short"))],
)
def test_bad_browser_keys_are_refused(p256dh: str, auth: str) -> None:
    with pytest.raises(ValueError):
        BrowserKeys(p256dh, auth).encrypt(b"hi")


def test_the_private_key_survives_saving() -> None:
    keys = VapidKeys.generate()

    loaded = VapidKeys.load(keys.private_text())

    assert loaded.public_text() == keys.public_text()
    assert len(unb64url(keys.public_text())) == 65


def test_the_signature_is_a_token_for_the_push_service() -> None:
    keys = VapidKeys.generate()

    header = keys.authorization(
        "https://fcm.googleapis.com/fcm/send/abc", "https://vpn.example.com", NOW
    )

    token, k = header.removeprefix("vapid t=").split(", k=")
    assert k == keys.public_text()
    head, claims, signature = token.split(".")
    assert json.loads(unb64url(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(unb64url(claims)) == {
        "aud": "https://fcm.googleapis.com",
        "exp": int((NOW + timedelta(hours=12)).timestamp()),
        "sub": "https://vpn.example.com",
    }
    raw = unb64url(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public_key(k).verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_a_signature_by_another_key_does_not_verify() -> None:
    header = VapidKeys.generate().authorization("https://push.example.com/x", "mailto:a@b.c", NOW)
    token = header.removeprefix("vapid t=").split(", k=")[0]
    head, claims, signature = token.split(".")
    raw = unb64url(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))

    with pytest.raises(InvalidSignature):
        public_key(VapidKeys.generate().public_text()).verify(
            der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())
        )
