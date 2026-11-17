"""A browser for tests: its keys, a subscription and decrypting what the panel pushed to it."""

import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apps.notifications.services.webpush import b64url, hkdf, public_bytes, public_key


@dataclass
class Browser:
    """A key pair and an auth secret, as a browser makes them for a subscription."""

    endpoint: str = "https://push.example.com/send/1"
    private: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    secret: bytes = field(default_factory=lambda: os.urandom(16))

    @property
    def p256dh(self) -> str:
        return b64url(public_bytes(self.private.public_key()))

    @property
    def auth(self) -> str:
        return b64url(self.secret)

    def form(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "p256dh": self.p256dh, "auth": self.auth}

    def decrypt(self, body: bytes) -> bytes:
        """The plain payload of an `aes128gcm` body (RFC 8291, the receiving side)."""
        salt, (_size, id_length) = body[:16], struct.unpack("!IB", body[16:21])
        sender = body[21 : 21 + id_length]
        shared = self.private.exchange(ec.ECDH(), public_key(b64url(sender)))
        own = public_bytes(self.private.public_key())
        ikm = hkdf(self.secret, shared, b"WebPush: info\x00" + own + sender, 32)
        key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(key).decrypt(nonce, body[21 + id_length :], None)
        return plain.removesuffix(b"\x02")

    def read(self, body: bytes) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.decrypt(body))
        return data
