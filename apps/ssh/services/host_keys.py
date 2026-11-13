"""Trust on first use of SSH host keys: the fingerprint saved per address, checked and forgotten."""

from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.models import KnownHost


class KnownHostVerifier:
    """Trust on first use: remember the first host key, refuse any other one later."""

    def verify(self, host: str, port: int, key_type: str, fingerprint: str) -> None:
        known, created = KnownHost.objects.get_or_create(
            host=host,
            port=port,
            defaults={"key_type": key_type, "fingerprint": fingerprint},
        )
        if created:
            return
        if (known.key_type, known.fingerprint) != (key_type, fingerprint):
            raise SshConnectionError(
                ErrorCode.HOST_KEY_MISMATCH,
                f"{host}:{port} expected {known.key_type} {known.fingerprint}, "
                f"got {key_type} {fingerprint}",
            )

    def trusted(self, host: str, port: int) -> KnownHost | None:
        return KnownHost.objects.filter(host=host, port=port).first()

    def forget(self, host: str, port: int) -> KnownHost | None:
        """Drop the saved key, so the next connection trusts the key the server has now."""
        known = self.trusted(host, port)
        if known is not None:
            known.delete()
        return known
