import base64

import pytest

from amnezia_core.errors import InvalidKeyError
from amnezia_core.keys import KeyPair, decode_key, generate_preshared_key

# RFC 7748 section 6.1 test vector (Alice), in WireGuard base64 form.
PRIVATE = base64.b64encode(
    bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
).decode()
PUBLIC = base64.b64encode(
    bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
).decode()


def test_public_key_is_derived_from_private_key() -> None:
    assert KeyPair.from_private_key(PRIVATE).public_key == PUBLIC


def test_generated_pair_is_consistent() -> None:
    pair = KeyPair.generate()

    assert KeyPair.from_private_key(pair.private_key) == pair
    assert len(decode_key(pair.public_key)) == 32


def test_repr_hides_private_key() -> None:
    pair = KeyPair.from_private_key(PRIVATE)

    assert PRIVATE not in repr(pair)
    assert PUBLIC in repr(pair)


def test_preshared_keys_are_random_32_bytes() -> None:
    first, second = generate_preshared_key(), generate_preshared_key()

    assert first != second
    assert len(decode_key(first)) == 32


@pytest.mark.parametrize("key", ["not base64!", base64.b64encode(b"short").decode()])
def test_decode_rejects_invalid_keys(key: str) -> None:
    with pytest.raises(InvalidKeyError):
        decode_key(key)
