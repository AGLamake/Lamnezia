"""Changing the port, subnet, DNS and obfuscation of a WireGuard-family instance (DESIGN.md 6.1).

AmneziaWG 2.0, legacy AmneziaWG and WireGuard. The Amnezia app reinstalls the container for
such a change and loses every client. The panel keeps the server keys and the peers: it edits
the config and restarts the interface, and for a new port moves the files into a new container,
since Docker cannot change a published port. Clients still have to import their keys again: the
old profiles carry the old settings. Turning the container log on or off moves the files into a
new container too; profiles stay. The move of an AmneziaWG 2.0 server to 3.1 works the same
way, with a fresh image. A new client subnet moves the container too, since start.sh takes the
subnet when the container starts; the server and every client keep their host part in it.
The special junk packets I1-I5 belong to the clients: the server keeps them in comments and is
not restarted for them, and old profiles keep connecting.
"""

from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from functools import partial
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Any, ClassVar

from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.awg import SPECIAL_JUNK, AwgServerConfig, Renumbering, same_for_server
from amnezia_core.awg_export import has_awg3_params
from amnezia_core.awg_install import upgraded_awg_params, without_timers
from amnezia_core.awg_masking import Masking
from amnezia_core.containers import AWG2, WgContainer
from apps.accounts.models import User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.keys.services.reissue import KeyReissue
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_container import AwgContainer
from apps.protocols.services.container import Transport
from apps.protocols.services.container_swap import ContainerSwap
from apps.protocols.signals import keys_changed
from apps.ssh.encryption import SecretCipher
from apps.ssh.services import RemoteHost

SETTINGS_KIND = "protocols.change_settings"
_HPK = "HeaderProtectionKey"
# Operation.SECRET_PARAMS: the key is kept out of the params and the audit journal.
_HPK_TOKEN = "header_protection_key_token"


def kept_files(kind: WgContainer) -> tuple[str, ...]:
    """Files of the container that carry its identity and clients (configure_container.sh).

    The config goes last: it marks the set complete, as in a restore from a backup.
    """
    return (
        kind.private_key_path,
        kind.public_key_path,
        kind.psk_path,
        kind.clients_table_path,
        kind.config_path,
    )


@dataclass(frozen=True)
class AwgSettings:
    """What an administrator can change; `params` holds only the parameters that are set.

    `logs`: docker keeps the container log. `subnet` is the client network, 10.8.1.0/24; empty
    in an operation queued before it could be changed, which keeps the one of the instance.
    """

    kind: ClassVar[str] = SETTINGS_KIND

    port: int
    params: dict[str, str]
    dns: tuple[str, str]
    logs: bool = False
    subnet: str = ""

    @classmethod
    def of(cls, instance: ProtocolInstance) -> "AwgSettings":
        return cls(
            instance.port,
            dict(instance.params),
            instance.dns,
            instance.container_logs,
            instance.subnet,
        )

    @classmethod
    def build(
        cls,
        port: int,
        params: Mapping[str, str],
        dns: tuple[str, str],
        *,
        logs: bool = False,
        subnet: str = "",
    ) -> "AwgSettings":
        values = {name: value.strip() for name, value in params.items()}
        params = {name: value for name, value in values.items() if value}
        return cls(port, params, dns, logs, subnet)

    @classmethod
    def from_params(cls, data: Mapping[str, Any]) -> "AwgSettings":
        primary, secondary = data["dns"]
        logs = data.get("logs", False)
        subnet = data.get("subnet", "")
        params = dict(data["params"])
        if token := data.get(_HPK_TOKEN):
            params[_HPK] = SecretCipher.from_settings().decrypt(token)
        return cls(data["port"], params, (primary, secondary), logs, subnet)

    def to_params(self) -> dict[str, Any]:
        """HeaderProtectionKey goes in encrypted, outside `params`: it is a secret."""
        params = dict(self.params)
        key = params.pop(_HPK, "")
        data: dict[str, Any] = {
            "port": self.port,
            "params": params,
            "dns": list(self.dns),
            "logs": self.logs,
            "subnet": self.subnet,
        }
        if key:
            data[_HPK_TOKEN] = SecretCipher.from_settings().encrypt(key)
        return data

    def within(self, current: "AwgSettings") -> "AwgSettings":
        """These settings with the subnet of `current` if they have none."""
        return self if self.subnet else replace(self, subnet=current.subnet)

    def changes_server(self, other: "AwgSettings") -> bool:
        """The port, the subnet or the obfuscation differ: the old profile does not connect.

        The timers and I1-I5 alone do not count: each side keeps its own timers, and the server
        does not check the special junk packets of a client.
        """
        mine = (self.port, self.subnet, _server_params(self.params))
        return mine != (other.port, other.subnet, _server_params(other.params))

    def changes_profiles(self, other: "AwgSettings") -> bool:
        """Clients must import their keys again; the container log is no concern of theirs."""
        return self.changes_server(other) or self.dns != other.dns

    def touches_server(self, other: "AwgSettings") -> bool:
        return self.changes_server(other) or self.params != other.params or self.logs != other.logs


def _server_params(params: Mapping[str, str]) -> dict[str, str]:
    """The parameters an old profile must match to connect."""
    return {
        name: value for name, value in without_timers(params).items() if name not in SPECIAL_JUNK
    }


class AwgReconfiguration:
    """Brings the settings to the config and the container under the server lock (5.4)."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, kind: WgContainer = AWG2) -> None:
        self._host = host
        self._ctx = ctx
        self._kind = kind
        self._docker = AwgContainer(host, ctx, kind)

    def apply(self, settings: AwgSettings) -> AwgServerConfig:
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            return self.apply_locked(settings)

    def apply_locked(self, settings: AwgSettings) -> AwgServerConfig:
        """As `apply`, on a server the caller has already locked."""
        with self._ctx.step(_("Read the server config")):
            text = self._docker.shell.read_file(self._kind.config_path).decode()
            config = AwgServerConfig.parse(text)
            before = config.serialize()
            new_port = config.listen_port != settings.port
            config.update_interface(settings.port, settings.params)
            renumbers = self._renumber(config, settings.subnet)
            moves = new_port or renumbers or self._docker.keeps_log() != settings.logs
        if moves:
            self._move(config, settings.port, logs=settings.logs, check_port=new_port)
        elif same_for_server(config.serialize(), before) and config.serialize() != before:
            with self._ctx.step(_("Save the server config")):
                self._docker.shell.write_file_atomic(
                    self._kind.config_path, config.serialize().encode()
                )
                self._ctx.log(_("Only I1-I5 change: the VPN interface keeps running."))
        elif config.serialize() != before:
            with self._ctx.step(_("Restart the VPN interface")):
                self._restart(config.serialize(), previous=text)
        return config

    def upgrade(self, port: int, masking: Masking | None = None) -> AwgServerConfig:
        """AmneziaWG 3.1 for a 2.0 server: a fresh image and the 3.1 parameters in the config.

        The server keys, the PSK and clientsTable move into the new container as they are; if it
        does not run with the new settings, the old container comes back. A server already on 3.1
        is left alone, so a retry after a failure in the panel only reads it. The app's I1 gives
        way to the `masking` template, a random one if None.
        """
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Read the server config")):
                text = self._docker.shell.read_file(self._kind.config_path).decode()
                config = AwgServerConfig.parse(text)
                upgraded = has_awg3_params(config.awg_params)
                if upgraded:
                    self._ctx.log(_("The server already runs AmneziaWG 3.1."))
            if upgraded:
                return config
            with self._ctx.step(_("Build the image with AmneziaWG 3.1")):
                # The running container keeps its old image until the new one replaces it.
                old_image = self._docker.image_id()
                self._docker.build_image()
            config.update_interface(port, upgraded_awg_params(config.awg_params, masking))
            self._move(config, port, logs=self._docker.keeps_log(), check_port=False)
            self._docker.remove_old_image(old_image)
        return config

    def _renumber(self, config: AwgServerConfig, subnet: str) -> bool:
        """Move the addresses of the config to `subnet`; False if they are there already."""
        current = config.address.network
        if not subnet or IPv4Network(subnet) == current:
            return False
        config.renumber(Renumbering(current, IPv4Network(subnet)))
        self._ctx.log(
            _("Client subnet: %(old)s → %(new)s.") % {"old": current, "new": config.address.network}
        )
        return True

    def _restart(self, text: str, *, previous: str) -> None:
        """Bring the interface up with the new file; if it does not come up, with the old one."""
        try:
            self._bring_up(text)
        except SetupError:
            with suppress(SetupError):
                self._bring_up(previous, check=False)
                self._ctx.log(_("The VPN interface runs with the old settings again."))
            raise

    def _bring_up(self, text: str, *, check: bool = True) -> None:
        self._docker.shell.write_file_atomic(self._kind.config_path, text.encode())
        self._docker.restart_awg(check=check)

    def _move(
        self,
        config: AwgServerConfig,
        port: int,
        *,
        logs: bool,
        check_port: bool,
    ) -> None:
        """A new container with the files of the old one; its own port needs no check.

        If the interface does not come up with the settings of the config, the old container
        comes back.
        """
        with self._ctx.step(_("Save the server files")):
            files = {
                path: self._docker.shell.read_file(path)
                for path in kept_files(self._kind)
                if self._docker.has_file(path)
            }
            files[self._kind.config_path] = config.serialize().encode()
        new_ports = [(port, Transport.UDP)] if check_port else []
        with ContainerSwap(self._host, self._ctx, self._docker).replace(new_ports):
            if not self._docker.has_image():
                self._docker.build_image()
            self._docker.run(port, logs=logs)
            self._docker.connect_network()
            for path, data in files.items():
                self._docker.shell.write_file_atomic(path, data)
            self._docker.start_awg(config.address.network)


class InstanceSettings:
    """The panel's side of a change: the instance, the key profiles, the owners."""

    def __init__(self, instance: ProtocolInstance, ctx: OperationContext) -> None:
        self._instance = instance
        self._ctx = ctx

    def save(self, settings: AwgSettings) -> list[AccessKey]:
        """Store the settings and update the profiles; returns the keys whose owners must know.

        A new subnet moves the address of every live key the way the server config moved.
        """
        instance = self._instance
        renumbering = self._renumbering(settings.subnet)
        with transaction.atomic():
            instance.port = settings.port
            instance.params = settings.params
            instance.primary_dns, instance.secondary_dns = settings.dns
            instance.container_logs = settings.logs
            instance.subnet = settings.subnet or instance.subnet
            instance.save(
                update_fields=[
                    "port",
                    "params",
                    "primary_dns",
                    "secondary_dns",
                    "container_logs",
                    "subnet",
                ]
            )
            if renumbering:
                self._renumber_keys(renumbering)

            def update(text: str) -> str:
                profile = ServerProfile.from_json(text)
                subnet = profile.subnet
                if renumbering:
                    subnet = str(renumbering.interface(IPv4Interface(subnet)))
                return profile.updated(
                    settings.port, settings.params, settings.dns, subnet
                ).to_json()

            return self.update_profiles(update)

    def _renumbering(self, subnet: str) -> Renumbering | None:
        current = self._instance.subnet
        if not subnet or not current or IPv4Network(subnet) == IPv4Network(current):
            return None
        return Renumbering(IPv4Network(current), IPv4Network(subnet))

    def _renumber_keys(self, renumbering: Renumbering) -> None:
        keys = [key for key in self.live_keys() if key.client_ip]
        for key in keys:
            key.client_ip = str(renumbering.address(IPv4Address(key.client_ip)))
        AccessKey.objects.bulk_update(keys, ["client_ip"])
        self._ctx.log(_("Key addresses moved: %(count)d.") % {"count": len(keys)})

    def update_profiles(self, update: Callable[[str], str]) -> list[AccessKey]:
        """Rewrite the stored profile of every live key; returns the keys."""
        keys = self.live_keys()
        for key in keys:
            if key.profile:
                key.profile = update(key.profile)
                key.save(update_fields=["profile"])
        self._ctx.log(_("Key profiles updated: %(count)d.") % {"count": len(keys)})
        return keys

    def reissue_imported(self, actor: User | None) -> list[AccessKey]:
        """Imported keys have no private key to rebuild the profile: they are issued anew,
        all of them by one operation of their own.

        Returns the keys that get a new one; an owner who may not have keys keeps the old.
        """
        if actor is None:
            self._ctx.log(_("Nobody to reissue the keys on behalf of."))
            return []
        started = KeyReissue(actor).start(key for key in self.live_keys() if key.imported)
        for key in started.kept:
            self._ctx.log(_("Key %(key)s is kept: its owner may not have keys.") % {"key": key})
        self._ctx.log(_("Keys to reissue: %(count)d.") % {"count": len(started.reissued)})
        return started.reissued

    def tell_owners(self, keys: list[AccessKey]) -> None:
        owners = {key.user for key in keys if key.user is not None}
        server = self._instance.server.name
        for owner in owners:
            transaction.on_commit(partial(keys_changed.send, type(self), user=owner, server=server))
        self._ctx.log(_("Owners notified: %(count)d.") % {"count": len(owners)})

    def live_keys(self) -> list[AccessKey]:
        source = self._instance.source
        return list(
            AccessKey.objects.filter(source=source)
            .exclude(status=KeyStatus.REVOKED)
            .select_related("user")
        )
