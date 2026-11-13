import pytest

from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.models import KnownHost
from apps.ssh.services import KnownHostVerifier

pytestmark = pytest.mark.django_db


def test_first_key_is_trusted_and_saved() -> None:
    KnownHostVerifier().verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:aaa")

    known = KnownHost.objects.get()
    assert (known.host, known.port, known.fingerprint) == ("vpn.example.com", 22, "SHA256:aaa")


def test_same_key_passes_again() -> None:
    verifier = KnownHostVerifier()
    verifier.verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:aaa")

    verifier.verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:aaa")

    assert KnownHost.objects.count() == 1


def test_other_key_is_refused() -> None:
    verifier = KnownHostVerifier()
    verifier.verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:aaa")

    with pytest.raises(SshConnectionError) as error:
        verifier.verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:bbb")

    assert error.value.code == ErrorCode.HOST_KEY_MISMATCH


def test_ports_are_separate_hosts() -> None:
    verifier = KnownHostVerifier()
    verifier.verify("vpn.example.com", 22, "ssh-ed25519", "SHA256:aaa")

    verifier.verify("vpn.example.com", 2222, "ssh-ed25519", "SHA256:bbb")

    assert KnownHost.objects.count() == 2
