"""WireGuard/AWG keys: X25519 pairs and preshared keys in base64, as `awg genkey` prints them."""

import base64
import binascii
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from amnezia_core.errors import InvalidKeyError

KEY_LENGTH = 32


def _encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode_key(key: str) -> bytes:
    """Raw 32 bytes of a base64 key; raises InvalidKeyError for anything else."""
    try:
        raw = base64.b64decode(key, validate=True)
    except binascii.Error as error:
        raise InvalidKeyError("Key is not valid base64.") from error
    if len(raw) != KEY_LENGTH:
        raise InvalidKeyError(f"Key must be {KEY_LENGTH} bytes, got {len(raw)}.")
    return raw


@dataclass(frozen=True, repr=False)
class KeyPair:
    """Client or server key pair. `repr` hides the private key so it never reaches logs."""

    private_key: str
    public_key: str

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls.from_private_key(_encode(X25519PrivateKey.generate().private_bytes_raw()))

    @classmethod
    def from_private_key(cls, private_key: str) -> "KeyPair":
        public = X25519PrivateKey.from_private_bytes(decode_key(private_key)).public_key()
        return cls(private_key, _encode(public.public_bytes(Encoding.Raw, PublicFormat.Raw)))

    def __repr__(self) -> str:
        return f"KeyPair(public_key={self.public_key!r})"


def generate_preshared_key() -> str:
    """Random 32-byte key, the equivalent of `awg genpsk`."""
    return _encode(secrets.token_bytes(KEY_LENGTH))
