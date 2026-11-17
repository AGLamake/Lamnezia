"""Web Push without a library: VAPID keys and signatures (RFC 8292) and the encrypted body
(RFC 8291, `aes128gcm` of RFC 8188). Plain Python on `cryptography`, no Django and no network.
"""

import base64
import json
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

_CURVE = ec.SECP256R1()
_POINT = serialization.PublicFormat.UncompressedPoint
_RAW = serialization.Encoding.X962
_COORDINATE = 32
# The body is one record: its size field only has to be larger than the record.
_RECORD_SIZE = 4096
_LAST_RECORD = b"\x02"
# A push service takes at least 4096 bytes of body; the header and the tag take 103 of them.
MAX_PAYLOAD = 3993
# A signature is good for at most 24 hours (RFC 8292 section 2).
_TOKEN_LIFETIME = timedelta(hours=12)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    """The key as browsers take it: the uncompressed point, 65 bytes."""
    return key.public_bytes(_RAW, _POINT)


@dataclass(frozen=True)
class VapidKeys:
    """The panel's key pair: browsers subscribe with the public key, the private one signs."""

    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> Self:
        return cls(ec.generate_private_key(_CURVE))

    @classmethod
    def load(cls, private: str) -> Self:
        """From `private_text()`."""
        value = int.from_bytes(unb64url(private), "big")
        return cls(ec.derive_private_key(value, _CURVE))

    def private_text(self) -> str:
        value = self.private_key.private_numbers().private_value
        return b64url(value.to_bytes(_COORDINATE, "big"))

    def public_text(self) -> str:
        """The `applicationServerKey` of a subscription and the `k` of the signature."""
        return b64url(public_bytes(self.private_key.public_key()))

    def authorization(self, endpoint: str, subject: str, now: datetime) -> str:
        """The `Authorization` header for a push to `endpoint`: a signed token for its origin."""
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int((now + _TOKEN_LIFETIME).timestamp()),
            "sub": subject,
        }
        header = b64url(_compact({"typ": "JWT", "alg": "ES256"}))
        signed = f"{header}.{b64url(_compact(claims))}"
        der = self.private_key.sign(signed.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(_COORDINATE, "big") + s.to_bytes(_COORDINATE, "big")
        return f"vapid t={signed}.{b64url(signature)}, k={self.public_text()}"


@dataclass(frozen=True)
class BrowserKeys:
    """The keys a browser gave with its subscription: `p256dh` and `auth`, base64url."""

    p256dh: str
    auth: str

    def encrypt(
        self,
        payload: bytes,
        *,
        salt: bytes | None = None,
        sender: ec.EllipticCurvePrivateKey | None = None,
    ) -> bytes:
        """The `aes128gcm` body for the browser; a fresh key pair of the panel for each message
        (`salt` and `sender` are given only by tests).

        Raises ValueError for keys that are not a P-256 point and a 16-byte secret.
        """
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("the payload is too long for a push")
        receiver = public_key(self.p256dh)
        secret = unb64url(self.auth)
        if len(secret) != 16:  # noqa: PLR2004 - the size RFC 8291 sets
            raise ValueError("the auth secret is not 16 bytes")
        sender = sender or ec.generate_private_key(_CURVE)
        sender_public = public_bytes(sender.public_key())
        shared = sender.exchange(ec.ECDH(), receiver)
        info = b"WebPush: info\x00" + public_bytes(receiver) + sender_public
        ikm = _hkdf(secret, shared, info, 32)
        salt = salt or os.urandom(16)
        key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        sealed = AESGCM(key).encrypt(nonce, payload + _LAST_RECORD, None)
        header = salt + struct.pack("!IB", _RECORD_SIZE, len(sender_public)) + sender_public
        return header + sealed


def public_key(text: str) -> ec.EllipticCurvePublicKey:
    """A P-256 public key from base64url of its uncompressed point; ValueError otherwise."""
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, unb64url(text))
    except (ValueError, TypeError) as error:
        raise ValueError("not a P-256 public key") from error


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 for one block of output, all RFC 8291 needs."""
    extract = HMAC(salt, hashes.SHA256())
    extract.update(ikm)
    expand = HMAC(extract.finalize(), hashes.SHA256())
    expand.update(info + b"\x01")
    return expand.finalize()[:length]


def _compact(data: dict[str, object]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()
