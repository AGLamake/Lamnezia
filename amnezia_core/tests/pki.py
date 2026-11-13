"""A tiny CA for tests: signs requests the way `easyrsa sign-req client` does."""

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.types import CertificatePublicKeyTypes
from cryptography.x509.oid import NameOID

# What `openssl ca` writes before the PEM block of pki/issued/<name>.crt.
TEXT_DUMP = "Certificate:\n    Data:\n        Version: 3 (0x2)\n"


class FakeCa:
    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Easy-RSA CA")])
        self._name = name
        self.certificate = self._build(name, self._key.public_key(), serial=1)

    @property
    def pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def sign(self, request_pem: str, serial: int) -> str:
        """pki/issued/<name>.crt: the text dump, then the PEM certificate."""
        request = x509.load_pem_x509_csr(request_pem.encode())
        certificate = self._build(request.subject, request.public_key(), serial)
        return TEXT_DUMP + certificate.public_bytes(serialization.Encoding.PEM).decode()

    def issue(self, common_name: str, serial: int) -> str:
        """A PEM certificate for a new key, as `certutil -S` makes one in the NSS database."""
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = self._build(subject, key.public_key(), serial)
        return certificate.public_bytes(serialization.Encoding.PEM).decode()

    def _build(
        self, subject: x509.Name, public_key: CertificatePublicKeyTypes, serial: int
    ) -> x509.Certificate:
        now = datetime.now(UTC)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._name)
            .public_key(public_key)
            .serial_number(serial)
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=825))
            .sign(self._key, hashes.SHA256())
        )
