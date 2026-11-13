"""Protocol and service fakes that also run the tar and cp commands of the backups."""

import io
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from amnezia_core.container_archive import (
    EXTRACT_COMMAND,
    FINISH_COMMAND,
    RESTORE_MARKER,
    SFTP_HOST_KEY_PATH,
    TOR_DATABASE_DUMP,
)
from amnezia_core.services import ONION_PATH, SFTP_CONTAINER, TOR_CONTAINER
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.protocols.tests.fake_ipsec_host import FakeIpsecHost
from apps.protocols.tests.fake_openvpn_host import FakeOpenVpnHost
from apps.protocols.tests.fake_proxy_host import FakeProxyHost
from apps.protocols.tests.fake_xray_host import FakeXrayHost
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

_ARCHIVE = "tar -czf - "
# `docker exec -i <name> sh -c <script>`: the place of the script.
_SCRIPT_WORD = 6
# The NSS database of the IPsec container: the fake keeps only whether it holds the CA.
NSS_DATABASE = "/etc/ipsec.d/cert9.db"
# The WordPress database of the Tor container, kept by the fake as one file of SQL.
SITE_DATABASE = "/var/lib/mysql/wp.sql"


def pack(files: dict[str, bytes], script: str) -> CommandResult:
    words = shlex.split(script[script.index(_ARCHIVE) :].partition(";")[0])
    folders = [f"/{folder}" for folder in words[words.index("-C") + 2 :]]
    dumped = TOR_DATABASE_DUMP.dump in script and SITE_DATABASE in files
    if dumped:
        files[TOR_DATABASE_DUMP.path] = files[SITE_DATABASE]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in sorted(files.items()):
            if any(path == folder or path.startswith(f"{folder}/") for folder in folders):
                info = tarfile.TarInfo(path.lstrip("/"))
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    if dumped:
        del files[TOR_DATABASE_DUMP.path]
    return result(stdout=buffer.getvalue())


def unpack(data: bytes) -> dict[str, bytes]:
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            assert extracted is not None
            files[f"/{member.name}"] = extracted.read()
    return files


def tar(files: dict[str, bytes], script: str, stdin: bytes) -> CommandResult | None:
    """The answer to a tar command of the backups, or None for any other script."""
    if _ARCHIVE in script:
        return pack(files, script)
    if script == EXTRACT_COMMAND:
        files[RESTORE_MARKER] = b""
        files.update(unpack(stdin))
        return result()
    if script == FINISH_COMMAND:
        files.pop(RESTORE_MARKER, None)
        return result()
    if script == TOR_DATABASE_DUMP.load_command():
        files[SITE_DATABASE] = files.pop(TOR_DATABASE_DUMP.path)
        return result()
    return None


def copy_file(files: dict[str, bytes] | None, command: str) -> CommandResult | None:
    """The answer to `docker cp name:path -` over the container files; None for other commands.

    `files` is None when there is no container. The file comes as a tar, as docker gives it.
    """
    words = shlex.split(command)
    if words[:2] == ["sh", "-c"]:
        words = shlex.split(words[2])
    if words[:2] != ["docker", "cp"]:
        return None
    path = words[2].partition(":")[2]
    if files is None or path not in files:
        return result(1, stderr=b"Error: Could not find the file in container\n")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(PurePosixPath(path).name)
        info.size = len(files[path])
        archive.addfile(info, io.BytesIO(files[path]))
    return result(stdout=buffer.getvalue())


@dataclass
class TarAwgServer(FakeAwgServer):
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        copied = copy_file(self.container_files if self.container_state else None, command)
        return copied or super().run(command, stdin=stdin, timeout=timeout)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.container_state == "running"
        return tar(self.container_files, script, stdin) or super()._in_container(script, stdin)


@dataclass
class TarOpenVpnHost(FakeOpenVpnHost):
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        copied = copy_file(self.files if self.container_state else None, command)
        return copied or super().run(command, stdin=stdin, timeout=timeout)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.container_state == "running"
        return tar(self.files, script, stdin) or super()._in_container(script, stdin)


@dataclass
class TarXrayHost(FakeXrayHost):
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        copied = copy_file(self.volume if self.container_state else None, command)
        return copied or super().run(command, stdin=stdin, timeout=timeout)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.container_state == "running"
        return tar(self.volume, script, stdin) or super()._in_container(script, stdin)


@dataclass
class TarIpsecHost(FakeIpsecHost):
    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        copied = copy_file(self.files if self.container_state else None, command)
        return copied or super().run(command, stdin=stdin, timeout=timeout)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.container_state == "running"
        if self.has_ca:
            self.files[NSS_DATABASE] = b"nss"
        answer = tar(self.files, script, stdin)
        if answer is None:
            return super()._in_container(script, stdin)
        self.has_ca = NSS_DATABASE in self.files
        return answer


def onion(letter: str) -> str:
    """An onion address made of one base32 letter."""
    return f"{letter * 56}.onion"


@dataclass
class TarServiceHost(FakeProxyHost):
    """Services of a fake host whose containers answer the tar and cp commands of the backups.

    Tor and sshd make keys of their own when their container is created, named by `own_key`,
    a base32 letter: two hosts with different letters are two different servers. `seeded`
    names the containers a copy was put into; `failing_extracts` makes as many of them fail
    before the files are in, as a dropped connection would.
    """

    own_key: str = "a"
    seeded: list[str] = field(default_factory=list)
    failing_extracts: int = 0

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        words = shlex.split(argv[2]) if argv[:2] == ["sh", "-c"] else []
        if words[:2] == ["docker", "cp"]:
            name = words[2].partition(":")[0]
            known = name in self.containers
            return copy_file(self.container_files(name) if known else None, command) or result(1)
        if words[:3] == ["docker", "exec", "-i"] and len(words) > _SCRIPT_WORD:
            answer = self._tar(words[3], words[_SCRIPT_WORD], stdin)
            if answer is not None:
                return answer
        return super().run(command, stdin=stdin, timeout=timeout)

    def container_files(self, name: str) -> dict[str, bytes]:
        texts = {**self.volumes.get(f"{name}-data", {}), **self.files.get(name, {})}
        return {path: text.encode() for path, text in texts.items()}

    def _tar(self, name: str, script: str, stdin: bytes) -> CommandResult | None:
        if script == EXTRACT_COMMAND and self.failing_extracts:
            self.failing_extracts -= 1
            self.put(name, RESTORE_MARKER, "")
            return result(2, stderr=b"tar: Unexpected EOF in archive\n")
        files = self.container_files(name)
        answer = tar(files, script, stdin)
        if answer is not None:
            assert self.containers[name].state == "running"
            if stdin:
                self.seeded.append(name)
            for path in self.container_files(name).keys() - files.keys():
                del self._store(name, path)[path]
            for path, content in files.items():
                self.put(name, path, content.decode())
        return answer

    def _docker(self, words: list[str]) -> CommandResult:
        answer = super()._docker(words)
        if words[1:2] == ["rm"]:
            self.files.pop(words[-1], None)
        if words[1:2] == ["run"]:
            name = words[words.index("--name") + 1]
            if name == TOR_CONTAINER:
                self.put(name, ONION_PATH, f"{onion(self.own_key)}\n")
            elif name == SFTP_CONTAINER:
                self.put(name, SFTP_HOST_KEY_PATH, f"host key {self.own_key}\n")
        return answer

    def _in_container(self, name: str, script: str) -> CommandResult:
        if name == TOR_CONTAINER and not script.startswith("[ -f "):
            assert self.containers[name].state == "running"
            return result(stdout=self.files[name][ONION_PATH].encode())
        return super()._in_container(name, script)
