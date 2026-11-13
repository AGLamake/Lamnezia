import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    MTPROXY_META_PATH,
    SECRET_PATH,
    SOCKS5_CONFIG_PATH,
    SOCKS5_CONTAINER,
    TELEMT_CONFIG_PATH,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
    TPROXY_META_PATH,
)
from apps.protocols.services.settings_backup import ARCHIVE_PATH
from apps.protocols.tests.fake_service_host import FakeServiceHost
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

SECRET = "0123456789abcdef0123456789abcdef"
RENEWED_SECRET = "fedcba9876543210fedcba9876543210"
# InstallController::buildRemoveContainerScript adds this loop for a container with a volume.
_VOLUME_LOOP = "\nfor attempt"
_FILE_MISSING_EXIT = 66
# What configure_container.sh of MTProxy writes to /data/mtproxy-meta, after substitution.
_META_LINE = re.compile(r"printf '(\w+)=%s\\n'\s+\"([^\"]*)\"")
# The lines of /data/config.toml of Telemt; lines under an `if [ -n "" ]` are left out.
_TOML_LINE = re.compile(r'^\s*echo "(.*)"$', re.M)


@dataclass
class FakeProxyHost(FakeServiceHost):
    """A host where configure_container.sh of the proxies writes what the app reads back.

    Files under /data live in the `<container>-data` volume and outlive the container; a new
    Telegram proxy gets `new_secret`, a renewed one `RENEWED_SECRET`. Downloads from Telegram
    fail unless `online` is on; the next `failing_configures` runs of the script and
    `failing_restarts` restarts fail.
    `scripts` keeps the configure scripts each container ran. A failed run has already removed
    the settings it was to write, as Telemt does with its config.toml. `archives` are the tar
    copies of the settings files (SettingsBackup) by container.
    """

    volumes: dict[str, dict[str, str]] = field(default_factory=dict)
    files: dict[str, dict[str, str]] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    scripts: dict[str, list[str]] = field(default_factory=dict)
    new_secret: str = SECRET
    online: bool = True
    failing_configures: int = 0
    failing_restarts: int = 0
    archives: dict[str, dict[str, str]] = field(default_factory=dict)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] == ["sh", "-c"] and _VOLUME_LOOP in argv[2]:
            removal = argv[2].partition(_VOLUME_LOOP)[0]
            super().run(shlex.join(["sh", "-c", removal]))
            volume = re.search(r"docker volume rm -f (\S+)", argv[2])
            assert volume
            self.volumes.pop(volume[1], None)
            return result(stdout=b"amnezia_volume_left=\n")
        if argv[:2] == ["sh", "-c"] and argv[2].startswith("docker exec -i "):
            words = shlex.split(argv[2])
            if "target=" in words[6]:
                # write_file_atomic in the container: start.sh, uploaded before it starts
                path = shlex.split(words[6].split("target=", 1)[1])[0]
                self.put(words[3], path, stdin.decode())
                return result()
        return super().run(command, stdin=stdin, timeout=timeout)

    def _docker(self, words: list[str]) -> CommandResult:
        if words[1:3] == ["exec", "-d"]:
            self.started.append(words[3])
            return result()
        if words[1:2] == ["restart"]:
            if self.failing_restarts:
                self.failing_restarts -= 1
                return result(1, stderr=b"Error response from daemon: cannot restart")
            self.restarted.append(words[2])
            return result()
        if words[1:2] == ["run"] and "-v" in words:
            self.volumes.setdefault(words[words.index("-v") + 1].partition(":")[0], {})
        return super()._docker(words)

    def _store(self, name: str, path: str) -> dict[str, str]:
        if path.startswith("/data/"):
            return self.volumes.setdefault(f"{name}-data", {})
        return self.files.setdefault(name, {})

    def put(self, name: str, path: str, text: str) -> None:
        self._store(name, path)[path] = text

    def _in_container(self, name: str, script: str) -> CommandResult:  # noqa: PLR0911
        assert self.containers[name].state == "running"
        if script.startswith("[ -f "):
            path = shlex.split(script)[2]
            text = self._store(name, path).get(path)
            return result(_FILE_MISSING_EXIT) if text is None else result(stdout=text.encode())
        if script.startswith(f"rm -f -- {ARCHIVE_PATH}"):
            self.archives.pop(name, None)
            return result()
        if script.startswith(("stat -c %s -- ", "rm -f -- ")):
            return self._file_command(name, script)
        if script.startswith("cd / && set -- && for path in "):
            return self._save_archive(name, script)
        if script.endswith(f"tar -xf {ARCHIVE_PATH} -C /"):
            return self._restore_archive(name, script)
        if name == SOCKS5_CONTAINER:
            return self._configure_socks5(script)
        return self._configure_telegram(name, script)

    def _configure_socks5(self, script: str) -> CommandResult:
        self.scripts.setdefault(SOCKS5_CONTAINER, []).append(script)
        users = re.search(r'echo -e "(users [^"]+)"', script)
        assert users and "auth strong" in script
        config = f"#!/bin/3proxy\n{users[1]}\nauth strong\n"
        self.put(SOCKS5_CONTAINER, SOCKS5_CONFIG_PATH, config)
        return result()

    def _paths(self, name: str) -> dict[str, str]:
        return {**self.files.get(name, {}), **self.volumes.get(f"{name}-data", {})}

    def _save_archive(self, name: str, script: str) -> CommandResult:
        roots = shlex.split(script.partition("for path in ")[2].partition("; do")[0])
        self.archives[name] = {
            path: text
            for path, text in self._paths(name).items()
            if any(path == f"/{root}" or path.startswith(f"/{root}/") for root in roots)
        }
        return result()

    def _restore_archive(self, name: str, script: str) -> CommandResult:
        for folder in re.findall(r"find (\S+) -mindepth 1 -delete", script):
            for path in [path for path in self._paths(name) if path.startswith(f"{folder}/")]:
                self._store(name, path).pop(path)
        for path, text in self.archives[name].items():
            self.put(name, path, text)
        return result()

    def _file_command(self, name: str, script: str) -> CommandResult:
        """`stat` gives the size of a file, `rm` deletes it."""
        path = shlex.split(script)[-1]
        store = self._store(name, path)
        if script.startswith("rm "):
            store.pop(path, None)
            return result()
        text = store.get(path)
        return result(1) if text is None else result(stdout=f"{len(text.encode())}\n".encode())

    def _configure_telegram(self, name: str, script: str) -> CommandResult:
        assert name in (MTPROXY_CONTAINER, TELEMT_CONTAINER, TPROXY_CONTAINER), name
        self.scripts.setdefault(name, []).append(script)
        if self.failing_configures:
            self.failing_configures -= 1
            for path in (MTPROXY_META_PATH, TELEMT_CONFIG_PATH, TPROXY_META_PATH):
                self._store(name, path).pop(path, None)
            return result(1, stdout=b"[!] configure failed")
        if not self.online:
            return result(1, stdout=b"[!] could not download proxy-secret\n")
        kept = self._store(name, SECRET_PATH).get(SECRET_PATH, self.new_secret).strip()
        secret = RENEWED_SECRET if 'if [ "1" = "1" ]' in script else kept
        if name == MTPROXY_CONTAINER:
            shell = dict(re.findall(r"^(\w+)=(\S*)$", script, re.M))
            meta = "".join(
                f"{key}={shell.get(value[1:], value) if value.startswith('$') else value}\n"
                for key, value in _META_LINE.findall(script)
            )
            self.put(name, MTPROXY_META_PATH, meta)
        elif name == TELEMT_CONTAINER:
            self.put(name, TELEMT_CONFIG_PATH, _telemt_config(script, secret))
        elif not self._configure_tproxy(name, script):
            return result(1)
        self.put(name, SECRET_PATH, f"{secret}\n")
        return result(stdout=f"[*] Secret:    {secret}\n".encode())

    def _configure_tproxy(self, name: str, script: str) -> bool:
        site = dict(re.findall(r"^(HOST|EMAIL|HTTPS_PORT)=(\S*)$", script, re.M))
        if not site.get("HOST") or not site.get("EMAIL"):
            return False
        meta = f"hostname={site['HOST']}\nemail={site['EMAIL']}\n"
        self.put(name, TPROXY_META_PATH, f"{meta}http_port=80\nhttps_port={site['HTTPS_PORT']}\n")
        return True


def _telemt_config(script: str, secret: str) -> str:
    """The config.toml the script echoes, with the secret and the extra users filled in."""
    block = script.partition("rm -f /data/config.toml")[2].partition("} > /data/config.toml")[0]
    lines = []
    for line in _TOML_LINE.findall(block):
        text = line.replace('\\"', '"')
        if text.endswith('= ""') and not text.startswith("[") and "SECRET" not in text:
            continue  # an optional value the script writes only when it is set
        lines.append(text.replace("$SECRET", secret))
    extras = re.search(r'for EXTRA in \$\(echo "([^"]*)"', script)
    for number, extra in enumerate(filter(None, (extras[1] if extras else "").split(",")), 1):
        lines.append(f'extra_{number} = "{extra}"')
    return "\n".join(line for line in lines if not line.startswith("extra_$i")) + "\n"
