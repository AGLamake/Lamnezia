"""What the import reads from the service containers, as the Amnezia app reads it.

Upstream extractConfigFromContainer of the service installers: the SFTP account is in the
`docker run` command of amnezia-sftp, the onion address in the Tor hidden service, the SOCKS5
account in 3proxy.cfg and the secret of a Telegram proxy in /data/secret with its settings next
to it. AmneziaDNS has nothing to read.
"""

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db import transaction

from amnezia_core.docker_ps import RunningContainer
from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    MTPROXY_META_PATH,
    SECRET_PATH,
    SOCKS5_CONFIG_PATH,
    SOCKS5_CONTAINER,
    SOCKS5_LOG_PATH,
    STANDARD,
    TELEMT_CONFIG_PATH,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
    TPROXY_META_PATH,
    TPROXY_PORT,
    Socks5Account,
    TelemtConfig,
    parse_meta,
    proxy_secret,
)
from amnezia_core.proxy_options import TelegramProxyOptions
from amnezia_core.services import (
    DNS_CONTAINER,
    DNS_FORWARD_RECORDS_PATH,
    ONION_PATH,
    SFTP_COMMAND_FORMAT,
    SFTP_CONTAINER,
    TOR_CONTAINER,
    SftpAccount,
    dns_logs_off,
    parse_onion,
)
from apps.keys.errors import explained_format_errors
from apps.protocols.models import (
    APP_LOG_SETTING,
    DNS_LOGS_SETTING,
    InstanceOrigin,
    ServiceInstance,
)
from apps.servers.models import Server
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost, ShellTarget


@dataclass(frozen=True, repr=False)
class ServiceFiles:
    """What one service container shows; `secret` is a password, the onion address or the
    secret of a Telegram proxy, `settings` what the links of a Telegram proxy need besides it.
    """

    container: str
    port: int | None
    login: str = ""
    secret: str = ""
    settings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, host: RemoteHost, running: RunningContainer) -> "ServiceFiles":
        name = running.name
        with explained_format_errors():
            if name == SFTP_CONTAINER:
                account = SftpAccount.from_command(
                    host.docker("inspect", "--format", SFTP_COMMAND_FORMAT, name)
                )
                return cls(name, running.port, account.user, account.password)
            if name == DNS_CONTAINER:
                records = read_text(host.container(name), DNS_FORWARD_RECORDS_PATH)
                settings = {} if dns_logs_off(records) else {DNS_LOGS_SETTING: "1"}
                return cls(name, running.port, settings=settings)
            if name == TOR_CONTAINER:
                hostname = host.container(name).run(f"cat {shlex.quote(ONION_PATH)}")
                return cls(name, running.port, secret=parse_onion(hostname))
            if proxy := cls._proxy_readers().get(name):
                return proxy(host.container(name), running)
        return cls(name, running.port)

    @classmethod
    def _proxy_readers(
        cls,
    ) -> dict[str, Callable[[ShellTarget, RunningContainer], "ServiceFiles"]]:
        return {
            SOCKS5_CONTAINER: cls._socks5,
            MTPROXY_CONTAINER: cls._mtproxy,
            TELEMT_CONTAINER: cls._telemt,
            TPROXY_CONTAINER: cls._tproxy,
        }

    @classmethod
    def _socks5(cls, shell: ShellTarget, running: RunningContainer) -> "ServiceFiles":
        """The account, and the size of the 3proxy.log the app left, which is not read."""
        size = file_size(shell, SOCKS5_LOG_PATH)
        settings = {} if size is None else {APP_LOG_SETTING: str(size)}
        account = Socks5Account.from_config(read_text(shell, SOCKS5_CONFIG_PATH))
        if not account:
            return cls(running.name, running.port, settings=settings)
        return cls(running.name, running.port, account.user, account.password, settings)

    @classmethod
    def _mtproxy(cls, shell: ShellTarget, running: RunningContainer) -> "ServiceFiles":
        meta = parse_meta(read_text(shell, MTPROXY_META_PATH))
        settings = {
            "mode": meta.get("mode") or STANDARD,
            "tls_domain": meta.get("domain", ""),
            "public_host": meta.get("public_host", ""),
            **option_settings(TelegramProxyOptions.from_mtproxy_meta(meta)),
        }
        secret = proxy_secret(read_text(shell, SECRET_PATH))
        return cls(running.name, running.port, secret=secret, settings=settings)

    @classmethod
    def _telemt(cls, shell: ShellTarget, running: RunningContainer) -> "ServiceFiles":
        config = TelemtConfig.parse(read_text(shell, TELEMT_CONFIG_PATH))
        settings = {
            "mode": config.mode,
            "tls_domain": config.tls_domain,
            "public_host": config.public_host,
            **option_settings(TelegramProxyOptions.from_telemt_config(config)),
        }
        return cls(
            running.name,
            config.port or running.port,
            config.user,
            proxy_secret(read_text(shell, SECRET_PATH)) or config.secret,
            settings,
        )

    @classmethod
    def _tproxy(cls, shell: ShellTarget, running: RunningContainer) -> "ServiceFiles":
        """`docker ps` shows port 80 of Caddy first, so the HTTPS port comes from the meta."""
        meta = parse_meta(read_text(shell, TPROXY_META_PATH))
        https_port = meta.get("https_port", "")
        return cls(
            running.name,
            int(https_port) if https_port.isdigit() else TPROXY_PORT,
            secret=proxy_secret(read_text(shell, SECRET_PATH)),
            settings={"hostname": meta.get("hostname", "")},
        )

    @transaction.atomic
    def save(self, server: Server, origin: InstanceOrigin) -> ServiceInstance:
        """Update the service the panel knows; one the panel installed stays "installed"."""
        snapshot = {
            "port": self.port,
            "login": self.login,
            "secret": self.secret,
            "settings": self.settings,
        }
        service, _created = ServiceInstance.objects.update_or_create(
            server=server,
            container=self.container,
            defaults=snapshot,
            create_defaults={**snapshot, "origin": origin},
        )
        return service


def option_settings(options: TelegramProxyOptions) -> dict[str, str]:
    """The settings of a Telegram proxy its links do not need, for its settings page."""
    return {
        "tag": options.tag,
        "additional_secrets": ",".join(options.additional_secrets),
        "nat_internal_ip": options.nat_internal_ip,
        "nat_external_ip": options.nat_external_ip,
        "workers": options.workers,
    }


def file_size(shell: ShellTarget, path: str) -> int | None:
    """Bytes of a file of the container, None if there is none; the file itself is not read."""
    result = shell.sh(f"stat -c %s -- {shlex.quote(path)}")
    size = result.output.strip()
    return int(size) if result.ok and size.isdigit() else None


def read_text(shell: ShellTarget, path: str) -> str:
    """A file of the container; a missing one reads as empty, as the app reads it."""
    try:
        return shell.read_file(path).decode(errors="replace")
    except RemoteFileNotFoundError:
        return ""
