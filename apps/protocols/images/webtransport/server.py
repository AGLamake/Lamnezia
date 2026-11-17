"""A WebTransport server the speed test page of the panel measures a port of the server with.

It runs in a temporary container (apps/protocols/services/speed_test.py), not in the panel:
`python server.py SECONDS TOKEN PORT...`. HTTP/3 goes over UDP, so the browser measures the
same UDP port an AmneziaWG client would use. The certificate is made here and never leaves the
container: the browser trusts it by the SHA-256 hash the panel reads from CERTIFICATE_HASH.

Only a session on the path /TOKEN is accepted. In it every bidirectional stream is one request:
- b"D" and a 4-byte size: the server sends that many bytes back (download);
- b"U" and any data: when the stream ends, the server answers with the 8-byte count (upload).
"""

import asyncio
import datetime
import hashlib
import struct
import sys
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated, QuicEvent, StreamReset
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERTIFICATE_HASH = Path("/tmp/certificate.sha256")
DOWNLOAD = b"D"
UPLOAD = b"U"
MAX_DOWNLOAD = 1 << 20
_DOWNLOAD_REQUEST = 5  # b"D" and the size
# Browsers take a certificate by its hash only if it is valid for at most 14 days.
_VALIDITY = datetime.timedelta(days=1)
_NAME = "lamnezia-speed-test"
_WINDOW = 16 << 20
_ZEROS = bytes(MAX_DOWNLOAD)


def certificate() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A self-signed ECDSA P-256 certificate: the only kind every browser takes by its hash."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _NAME)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + _VALIDITY)
        # Chrome cannot parse a certificate without extensions.
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_NAME)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


class SpeedTestProtocol(QuicConnectionProtocol):
    """One browser connection: the session on the right path and its requests."""

    token = ""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._http: H3Connection | None = None
        self._sessions: set[int] = set()
        self._requests: dict[int, bytearray] = {}
        self._uploaded: dict[int, int] = {}

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated) and event.alpn_protocol in H3_ALPN:
            self._http = H3Connection(self._quic, enable_webtransport=True)
        elif isinstance(event, StreamReset):
            self._requests.pop(event.stream_id, None)
            self._uploaded.pop(event.stream_id, None)
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self._handle(http_event)

    def _handle(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            self._open(event)
        elif (
            isinstance(event, WebTransportStreamDataReceived) and event.session_id in self._sessions
        ):
            self._receive(event.stream_id, event.data, event.stream_ended)

    def _open(self, event: HeadersReceived) -> None:
        assert self._http is not None
        headers = dict(event.headers)
        accepted = (
            headers.get(b":method") == b"CONNECT"
            and headers.get(b":protocol") == b"webtransport"
            and headers.get(b":path") == f"/{self.token}".encode()
        )
        if accepted:
            self._sessions.add(event.stream_id)
            status = [(b":status", b"200"), (b"sec-webtransport-http3-draft", b"draft02")]
        else:
            status = [(b":status", b"404")]
        self._http.send_headers(event.stream_id, status, end_stream=not accepted)
        self.transmit()

    def _receive(self, stream_id: int, data: bytes, ended: bool) -> None:
        if stream_id in self._uploaded:
            self._uploaded[stream_id] += len(data)
        else:
            request = self._requests.setdefault(stream_id, bytearray())
            request.extend(data)
            if request[:1] == UPLOAD:
                self._uploaded[stream_id] = len(request) - 1
                del self._requests[stream_id]
            elif request[:1] == DOWNLOAD and len(request) >= _DOWNLOAD_REQUEST:
                del self._requests[stream_id]
                (size,) = struct.unpack("!I", request[1:_DOWNLOAD_REQUEST])
                self._answer(stream_id, _ZEROS[: min(size, MAX_DOWNLOAD)])
                return
            elif request[:1] not in (b"", DOWNLOAD, UPLOAD):
                del self._requests[stream_id]
                self._answer(stream_id, b"")
                return
        if ended and stream_id in self._uploaded:
            self._answer(stream_id, struct.pack("!Q", self._uploaded.pop(stream_id)))
        elif ended:
            self._requests.pop(stream_id, None)

    def _answer(self, stream_id: int, data: bytes) -> None:
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()


async def main(seconds: int, token: str, ports: list[int]) -> None:
    cert, key = certificate()
    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=False,
        max_datagram_frame_size=65536,
        max_data=_WINDOW,
        max_stream_data=_WINDOW,
    )
    configuration.certificate = cert
    configuration.private_key = key
    SpeedTestProtocol.token = token
    for port in ports:
        await serve("0.0.0.0", port, configuration=configuration, create_protocol=SpeedTestProtocol)
    CERTIFICATE_HASH.write_text(
        hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    )
    await asyncio.sleep(seconds)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], [int(port) for port in sys.argv[3:]]))
