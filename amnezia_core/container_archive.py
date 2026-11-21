"""Saved state of a protocol container: the folders that make it the same server, as a tar.gz.

The folders are those the upstream server scripts and the Amnezia app write: the keys, the
certificate authority, the configuration and clientsTable; for a service, the onion keys and
the WordPress site of Tor, the secret of a Telegram proxy or the SSH host keys and uploads of
SFTP. Put back byte for byte into a new container, they let the app and every issued key find
the server as it was.
"""

import base64
import binascii
import gzip
import hashlib
import io
import shlex
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from amnezia_core import ipsec, openvpn
from amnezia_core.awg import AwgServerConfig
from amnezia_core.containers import AWG_LEGACY, WIREGUARD
from amnezia_core.errors import AmneziaFormatError
from amnezia_core.ipsec import IPSEC_CA_CERT_PATH, IPSEC_CONTAINER, IPSEC_DIR
from amnezia_core.openvpn import OPENVPN_CA_CERT_PATH, OPENVPN_CONTAINER, OPENVPN_DIR
from amnezia_core.proxies import MTPROXY_CONTAINER, SECRET_PATH, TELEMT_CONTAINER, proxy_secret
from amnezia_core.services import (
    ONION_PATH,
    SFTP_CONTAINER,
    SFTP_HOME,
    TOR_CONTAINER,
    TOR_DATABASE,
    TOR_DATABASE_PASSWORD,
    TOR_DATABASE_USER,
    TOR_SITE_DIR,
    parse_onion,
)
from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_CONTAINER, XRAY_DIR, XrayServerConfig

# Certificates and keys of a few thousand clients are a few megabytes; far more is not a copy.
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024
# A service that keeps what its users put there: the Tor site, the SFTP uploads.
USER_FILES_MAX_SIZE = 256 * 1024 * 1024
# Seconds to read such a copy over SSH.
USER_FILES_TIMEOUT = 600
# Seconds between the attempts to load a dump while the database starts, and their number.
_LOAD_PAUSE = 2
_LOAD_ATTEMPTS = 30
# Left in a container while a copy goes into it: one that still has it is a restore that did
# not finish, with no keys or only some of them.
RESTORE_MARKER = "/.lamnezia-restore"
# A container a restore runs carries this label from `docker run` on, and gets the file once
# the copy is all in: a labelled one without the file is a restore whose worker stopped
# before the copy, even before the marker was written.
RESTORE_LABEL = "lamnezia.restore"
RESTORE_DONE = "/.lamnezia-restored"
# Unpacks a copy into the container over the paths it holds, and the end of the restore.
EXTRACT_COMMAND = f"touch {RESTORE_MARKER} && tar -xzf - -C /"
FINISH_COMMAND = f"rm -f {RESTORE_MARKER} && touch {RESTORE_DONE}"
# A time far in the past given to the dump and its folder, so an unchanged database leaves
# the fingerprint of the files as it was.
_PINNED_TIME = "197001010000"
_ALLOWED_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
_SHA256_LENGTH = 64
_DIGEST_CHUNK = 1024 * 1024


class ArchiveFormatError(AmneziaFormatError):
    """A copy of container files is not a tar.gz of plain files in the folders of its protocol."""


class ArchiveTooLargeError(ArchiveFormatError):
    """The files of the container are more than its copy may hold; `size` is in bytes."""

    def __init__(self, size: int) -> None:
        super().__init__("the copy is too large")
        self.size = size


@dataclass(frozen=True)
class StateFingerprint:
    """What the container holds now, without copying it: the size and a hash of the files.

    The hash covers the paths, contents, owners and times of the files, so a copy is taken
    again only when one of them changed. `size` is what the folders take on disk, in bytes.
    """

    size: int
    digest: str

    @classmethod
    def parse(cls, output: str) -> "StateFingerprint":
        """The output of `ContainerState.fingerprint_command`: kilobytes and the hash."""
        match output.split():
            case [kilobytes, digest] if kilobytes.isdigit() and len(digest) == _SHA256_LENGTH:
                return cls(int(kilobytes) * 1024, digest)
        raise ArchiveFormatError("the fingerprint of the container cannot be read")


@dataclass(frozen=True)
class DatabaseDump:
    """A database of the container, dumped into a file of the copy and loaded back from it.

    `dump` writes the SQL to stdout, `load` reads it from stdin; both run in the container.
    The file lives only in the copy: it is removed after the archive and after the load.
    """

    path: str
    dump: str
    load: str

    def around(self, archive: str, *, pinned: bool = False) -> str:
        """The archive command with the dump written before it; the exit code is the archive's.

        `pinned` gives the dump and its folder a fixed time, for a fingerprint of the files.
        """
        folder = shlex.quote(str(PurePosixPath(self.path).parent))
        path = shlex.quote(self.path)
        pin = f" && touch -t {_PINNED_TIME} {path} {folder}" if pinned else ""
        return (
            f"mkdir -p {folder} && {self.dump} > {path}{pin} && {archive}; "
            f"status=$?; rm -f {path}; exit $status"
        )

    def load_command(self) -> str:
        """Loads the file once the database takes it: after a restart it may still be starting."""
        path = shlex.quote(self.path)
        return (
            f"for _ in $(seq {_LOAD_ATTEMPTS}); do "
            f"{self.load} < {path} && rm -f {path} && exit 0; sleep {_LOAD_PAUSE}; done; exit 1"
        )

    @property
    def load_timeout(self) -> int:
        """Seconds the load command may take, with a margin for the load itself."""
        return 2 * _LOAD_ATTEMPTS * _LOAD_PAUSE


@dataclass(frozen=True)
class ContainerState:
    """Where one protocol container keeps its state and how its files name the server.

    `identity` reads the file at `identity_path` and returns what the panel stores for the
    server: the private key of WireGuard and XRay, the fingerprint of the OpenVPN or IPsec CA.
    `directories` may name single files too. With `skip_missing` a folder or file the
    container lacks is left out of the copy; `database` is dumped into it.
    """

    container: str
    directories: tuple[str, ...]
    identity_path: str
    identity: Callable[[str], str]
    database: DatabaseDump | None = None
    skip_missing: bool = False
    max_size: int = MAX_ARCHIVE_SIZE
    # Seconds to read the copy; None is the timeout of any command.
    timeout: float | None = None

    def archive_command(self) -> str:
        """The tar command that writes the folders to stdout, with paths relative to /.

        The dump is pinned as for the fingerprint, so `ContainerArchive.digest` of the copy is
        the fingerprint the same files give.
        """
        archive = shlex.join(["tar", "-czf", "-", *self._tar_options()])
        return self.database.around(archive, pinned=True) if self.database else archive

    def fingerprint_command(self) -> str:
        """Prints the kilobytes of the folders and a SHA-256 of their uncompressed tar.

        Only the two words of `StateFingerprint` leave the server: the files are read there,
        so a large copy is not sent while it has not changed.
        """
        size = (
            f"du -sk {shlex.join(self.directories)} 2>/dev/null"
            " | awk '{s += $1} END {print s + 0}'"
        )
        digest = shlex.join(["tar", "-cf", "-", *self._tar_options()])
        command = (
            f'kb=$({size}) && sum=$({digest} 2>/dev/null | sha256sum) && echo "$kb ${{sum%% *}}"'
        )
        return self.database.around(command, pinned=True) if self.database else command

    def _tar_options(self) -> list[str]:
        relative = [directory.lstrip("/") for directory in self.directories]
        options = ["--ignore-failed-read"] if self.skip_missing else []
        return [*options, "-C", "/", *relative]

    def holds(self, path: str) -> bool:
        """Whether an absolute path lies in one of the state folders."""
        return any(path == d or path.startswith(f"{d}/") for d in self.directories)


@dataclass(frozen=True, repr=False)
class ContainerArchive:
    """A checked copy of the state folders of one container; `repr` hides the secrets in it."""

    state: ContainerState
    data: bytes

    def __post_init__(self) -> None:
        self.files()  # refuse a copy that is not ours before it is kept or unpacked

    @classmethod
    def decode(cls, state: ContainerState, text: str) -> "ContainerArchive":
        """The copy as the panel stores it: base64 of the tar.gz."""
        try:
            return cls(state, base64.b64decode(text, validate=True))
        except binascii.Error as error:
            raise ArchiveFormatError("the saved copy is not base64") from error

    def encode(self) -> str:
        return base64.b64encode(self.data).decode()

    def files(self) -> dict[str, bytes]:
        """Absolute paths and contents of the files in the copy."""
        try:
            with tarfile.open(fileobj=io.BytesIO(self.data), mode="r:gz") as archive:
                return self._read(archive)
        except (tarfile.TarError, EOFError, OSError) as error:
            name = self.state.container
            raise ArchiveFormatError(f"the copy of {name} cannot be read") from error

    def digest(self) -> str:
        """The SHA-256 of the uncompressed tar: what `fingerprint_command` prints for these files.

        `tar -czf` gzips the same tar stream `tar -cf` writes, so the copy tells whether the
        files changed without the server reading them again.
        """
        digest = hashlib.sha256()
        with gzip.GzipFile(fileobj=io.BytesIO(self.data)) as tar:
            while chunk := tar.read(_DIGEST_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()

    def identity(self) -> str:
        """The server key or CA fingerprint the files of the copy hold."""
        content = self.files().get(self.state.identity_path)
        if content is None:
            raise ArchiveFormatError(f"the copy has no {self.state.identity_path}")
        return self.state.identity(content.decode())

    def _read(self, archive: tarfile.TarFile) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        total = 0
        for member in archive:
            path = self._checked_path(member)
            total += member.size
            if total > self.state.max_size:
                raise ArchiveTooLargeError(total)
            if member.isfile():
                extracted = archive.extractfile(member)
                files[path] = extracted.read() if extracted else b""
        return files

    def _checked_path(self, member: tarfile.TarInfo) -> str:
        if member.type not in _ALLOWED_TYPES:
            raise ArchiveFormatError(f"{member.name} is not a plain file or folder")
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArchiveFormatError(f"{member.name} points outside the container folders")
        path = f"/{relative}"
        if not self.state.holds(path):
            raise ArchiveFormatError(f"{member.name} is not in the folders of the protocol")
        return path


def _wg_private_key(text: str) -> str:
    return AwgServerConfig.parse(text).private_key or ""


def _xray_private_key(text: str) -> str:
    return XrayServerConfig.parse(text).private_key


def _fingerprint(text: str) -> str:
    """The SFTP host key is known to no one but the clients: its hash only marks the copy."""
    return hashlib.sha256(text.encode()).hexdigest()


AWG_LEGACY_STATE = ContainerState(
    AWG_LEGACY.name, (AWG_LEGACY.directory,), AWG_LEGACY.config_path, _wg_private_key
)
WIREGUARD_STATE = ContainerState(
    WIREGUARD.name, (WIREGUARD.directory,), WIREGUARD.config_path, _wg_private_key
)
# The data volume: server.json, clientsTable and the Reality keys of configure_container.sh.
XRAY_STATE = ContainerState(XRAY_CONTAINER, (XRAY_DIR,), XRAY_CONFIG_PATH, _xray_private_key)
# easy-rsa pki/ (CA key, index.txt, serial, CRL, issued certificates), server.conf, ta.key and
# the client requests: without them no issued profile connects to a new container.
OPENVPN_STATE = ContainerState(
    OPENVPN_CONTAINER, (OPENVPN_DIR,), OPENVPN_CA_CERT_PATH, openvpn.ca_fingerprint
)
# The NSS database with the CA, the server and client certificates and the CRL, ikev2.conf,
# and the client files and clientsTable of the app.
IPSEC_STATE = ContainerState(
    IPSEC_CONTAINER, ("/etc/ipsec.d", IPSEC_DIR), IPSEC_CA_CERT_PATH, ipsec.ca_fingerprint
)
# The keys of the hidden service, from which the onion address of the site comes, and the
# WordPress site as a whole: the core the site was updated to, its content, wp-config.php
# with the salts of the logins, and a dump of the database. The core of the image would be
# older than the database an updated site left.
TOR_DATABASE_DUMP = DatabaseDump(
    "/var/lib/lamnezia/wordpress.sql",
    dump=(
        f"MYSQL_PWD={TOR_DATABASE_PASSWORD} mysqldump -u{TOR_DATABASE_USER} "
        f"--single-transaction --no-tablespaces --skip-dump-date {TOR_DATABASE}"
    ),
    load=f"MYSQL_PWD={TOR_DATABASE_PASSWORD} mysql -u{TOR_DATABASE_USER} {TOR_DATABASE}",
)
TOR_STATE = ContainerState(
    TOR_CONTAINER,
    (
        str(PurePosixPath(ONION_PATH).parent),
        TOR_SITE_DIR,
        str(PurePosixPath(TOR_DATABASE_DUMP.path).parent),
    ),
    ONION_PATH,
    parse_onion,
    database=TOR_DATABASE_DUMP,
    skip_missing=True,
    max_size=USER_FILES_MAX_SIZE,
    timeout=USER_FILES_TIMEOUT,
)
# The data volume of the Telegram proxies: the secret the links carry and the settings.
MTPROXY_STATE = ContainerState(MTPROXY_CONTAINER, ("/data",), SECRET_PATH, proxy_secret)
TELEMT_STATE = ContainerState(TELEMT_CONTAINER, ("/data",), SECRET_PATH, proxy_secret)
# The SSH host keys atmoz/sftp makes on its first start, with which clients see the same
# server, and the home folders with the uploaded files. The account is in the `docker run`
# command.
SFTP_HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key"
SFTP_STATE = ContainerState(
    SFTP_CONTAINER,
    ("/etc/ssh", SFTP_HOME),
    SFTP_HOST_KEY_PATH,
    _fingerprint,
    max_size=USER_FILES_MAX_SIZE,
    timeout=USER_FILES_TIMEOUT,
)
