"""Changing the port, masking site, SNI, flow and fingerprint of XRay (VLESS Reality over RAW).

As in the Amnezia app, the fingerprint and the `-udp443` flow belong to the client profiles
only, the rest goes to server.json, which is rewritten by the client protocol of
XrayClientManager: clients, traffic statistics and the speed marks stay, the counters are read
before the restart. A new port or container log needs a new container: the data volume with the
keys and server.json passes to it, the old one waits aside until xray serves.
"""

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, ClassVar

from django.utils.translation import gettext as _

from amnezia_core.xray import XRAY_CONFIG_PATH, XrayServerConfig, XraySettings
from apps.keys.errors import explained_format_errors
from apps.keys.services.profile import XrayKeyProfile
from apps.keys.services.xray_clients import XrayClientManager
from apps.keys.signals import xray_restarting
from apps.operations.services.context import OperationContext
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.container import Transport, upstream_file
from apps.protocols.services.container_swap import ContainerSwap
from apps.protocols.services.xray_container import XRAY_FOLDER, XrayDocker
from apps.protocols.services.xray_installer import XrayInstaller
from apps.ssh.services import RemoteHost

XRAY_SETTINGS_KIND = "protocols.change_xray_settings"
_FILE_MODE = 0o600


@dataclass(frozen=True)
class XrayPanelSettings:
    """What an administrator can change; an empty SNI is the site, as upstream."""

    kind: ClassVar[str] = XRAY_SETTINGS_KIND

    server: XraySettings
    logs: bool = False

    @classmethod
    def of(cls, instance: ProtocolInstance) -> "XrayPanelSettings":
        params = instance.params
        defaults = XraySettings()
        server = XraySettings(
            port=instance.port,
            flow=str(params.get("flow", defaults.flow)),
            site=str(params.get("site") or defaults.site),
            sni=str(params.get("sni") or params.get("site") or defaults.sni),
            fingerprint=str(params.get("fingerprint") or defaults.fingerprint),
        )
        return cls(server, instance.container_logs)

    @classmethod
    def build(  # noqa: PLR0913 - the fields of the form
        cls, *, port: int, flow: str, site: str, sni: str, fingerprint: str, logs: bool = False
    ) -> "XrayPanelSettings":
        site = site.strip()
        server = XraySettings(port, flow, site, sni.strip() or site, fingerprint)
        return cls(server, logs)

    @classmethod
    def from_params(cls, data: Mapping[str, Any]) -> "XrayPanelSettings":
        server = XraySettings(
            data["port"], data["flow"], data["site"], data["sni"], data["fingerprint"]
        )
        return cls(server, bool(data.get("logs", False)))

    def to_params(self) -> dict[str, Any]:
        server = self.server
        return {
            "port": server.port,
            "flow": server.flow,
            "site": server.site,
            "sni": server.sni,
            "fingerprint": server.fingerprint,
            "logs": self.logs,
        }

    def store(self, instance: ProtocolInstance) -> None:
        """`params` as the import stores them; the shortId stays, the settings keep it."""
        server = self.server
        instance.port = server.port
        instance.params = {
            "short_id": instance.params.get("short_id", ""),
            "site": server.site,
            "sni": server.sni,
            "flow": server.flow,
            "fingerprint": server.fingerprint,
        }
        instance.container_logs = self.logs
        instance.save(update_fields=["port", "params", "container_logs"])

    def changes_server(self, other: "XrayPanelSettings") -> bool:
        """server.json differs: xray restarts and every client reconnects."""
        return self._server_side() != other._server_side()

    def changes_profiles(self, other: "XrayPanelSettings") -> bool:
        """Clients must import their keys again; the container log is no concern of theirs."""
        return self.server != other.server

    def touches_server(self, other: "XrayPanelSettings") -> bool:
        return self.changes_server(other) or self.logs != other.logs

    def updated_profile(self, text: str) -> str:
        server = self.server
        profile = XrayKeyProfile.from_json(text)
        return replace(
            profile,
            port=server.port,
            flow=server.flow,
            site=server.site,
            sni=server.sni,
            fingerprint=server.fingerprint,
        ).to_json()

    def _server_side(self) -> tuple[int, str, str, str]:
        server = self.server
        return server.port, server.server_flow, server.site, server.sni


class XrayReconfiguration:
    """Brings the settings to server.json and the container (DESIGN.md 5.4).

    `source` is the instance the traffic collector reads the counters of before a restart.
    """

    def __init__(self, host: RemoteHost, ctx: OperationContext, source: str) -> None:
        self._host = host
        self._ctx = ctx
        self._source = source
        self._docker = XrayDocker(host, ctx)

    def apply(self, settings: XrayPanelSettings) -> None:
        with self._ctx.step(_("Read the server config")):
            current = self._read_config().settings()
            moves = current.port != settings.server.port
            moves = moves or self._docker.keeps_log() != settings.logs
        if moves:
            self._move(settings, check_port=current.port != settings.server.port)
            return
        manager = XrayClientManager(self._host, self._ctx, self._source)
        manager.update_config(lambda config: config.apply_settings(settings.server))

    def _move(self, settings: XrayPanelSettings, *, check_port: bool) -> None:
        """A new container on the data volume; the old one comes back with its server.json."""
        server = settings.server
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Read the server config")):
                text = self._docker.shell.read_file(XRAY_CONFIG_PATH).decode()
                config = self._read_config(text)
                config.apply_settings(server)
                config.enable_stats()
                if self._source:
                    xray_restarting.send_robust(type(self), source=self._source, host=self._host)
            installer = XrayInstaller(
                self._host, self._ctx, port=server.port, site=server.site, logs=settings.logs
            )
            published = installer.published_ports(server.port) if check_port else set()
            new_ports = [(port, Transport(transport)) for port, transport in published]
            swap = ContainerSwap(self._host, self._ctx, self._docker)
            with swap.replace(new_ports, restore=partial(self._restore, text)):
                if not self._docker.has_image():
                    self._docker.build(upstream_file(f"{XRAY_FOLDER}/Dockerfile"))
                self._docker.run_container(installer.run_arguments())
                self._docker.connect_network()
                self._write_config(config.serialize())
                installer.start(installer.configure())

    def _restore(self, text: str) -> None:
        """The volume is shared: the old container gets its own server.json back."""
        self._write_config(text)
        self._docker.restart()

    def _write_config(self, text: str) -> None:
        self._docker.shell.write_file_atomic(XRAY_CONFIG_PATH, text.encode(), _FILE_MODE)

    def _read_config(self, text: str | None = None) -> XrayServerConfig:
        if text is None:
            text = self._docker.shell.read_file(XRAY_CONFIG_PATH).decode()
        with explained_format_errors():
            config = XrayServerConfig.parse(text)
            config.settings()
        return config
