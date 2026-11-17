import datetime
import socket
import ssl
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from apps.notifications.services.pinned import PinnedAddresses

NAME = "hooks.example.com"


def _certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(NAME)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Answer(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.headers["Host"].encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def tls_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """An HTTPS server on the loopback with a certificate for NAME, trusted by the client."""
    cert_path, key_path = _certificate(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), _Answer)
    server.timeout = 2
    server_side = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_side.load_cert_chain(cert_path, key_path)
    server.socket = server_side.wrap_socket(server.socket, server_side=True)
    client_side = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(ssl, "create_default_context", lambda: client_side)
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    yield server.server_port
    thread.join()
    server.server_close()


def test_https_goes_to_the_pinned_address_and_checks_the_certificate_by_name(
    tls_server: int,
) -> None:
    opener = PinnedAddresses(["127.0.0.1"]).opener()

    with opener.open(f"https://{NAME}:{tls_server}/", timeout=5) as response:
        assert response.read().decode() == f"{NAME}:{tls_server}"


def test_a_certificate_for_another_name_is_refused(tls_server: int) -> None:
    opener = PinnedAddresses(["127.0.0.1"]).opener()

    with pytest.raises(urllib.error.URLError) as error:
        opener.open(f"https://other.example.com:{tls_server}/", timeout=5)

    assert isinstance(error.value.reason, ssl.SSLCertVerificationError)


def test_the_next_address_is_tried_when_one_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[tuple[str, int]] = []
    connection = object()

    def connect(address: tuple[str, int], timeout: float) -> object:
        tried.append(address)
        if address[0] == "2001:db8::1":
            raise ConnectionRefusedError("refused")
        return connection

    monkeypatch.setattr(socket, "create_connection", connect)

    answered = PinnedAddresses(["2001:db8::1", "8.8.8.8"]).connect(443, 5)

    assert answered is connection
    assert tried == [("2001:db8::1", 443), ("8.8.8.8", 443)]


def test_the_last_error_is_raised_when_no_address_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(address: tuple[str, int], timeout: float) -> object:
        raise ConnectionRefusedError(address[0])

    monkeypatch.setattr(socket, "create_connection", refuse)

    with pytest.raises(ConnectionRefusedError, match=r"8\.8\.8\.8"):
        PinnedAddresses(["2001:db8::1", "8.8.8.8"]).connect(443, 5)


def test_nothing_to_pin_is_an_error() -> None:
    with pytest.raises(ValueError):
        PinnedAddresses([])
