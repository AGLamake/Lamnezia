"""Reissuing the IPsec server certificate for a new server IP, and the key profiles after it.

The certificate of the server is issued for its IP: the nickname, the common name and the SAN
(upstream configure_container.sh), and `leftid` of ikev2.conf names it. Clients check it, so a
server moved to another IP, or restored on a VPS with another one, refuses them. The CA stays,
and with it every client certificate: the panel signs a server certificate for the new IP with
the same CA, points ikev2.conf at it and starts pluto again. The key profiles carry the server
IP, so the owners import their keys again.
"""

import shlex
from contextlib import ExitStack
from dataclasses import dataclass, replace

from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.ipsec import (
    IKEV2_CONFIG_PATH,
    IPSEC_CONTAINER,
    NSS_DATABASE,
    server_certificate_options,
    with_server_identity,
)
from amnezia_core.openvpn import is_ipv4
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import IpsecKeyProfile
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.installer import resolve_server_ip
from apps.protocols.services.ipsec_container import IpsecDocker
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

REISSUE_KIND = "protocols.ipsec_reissue"
_NSS = ("-d", NSS_DATABASE)


@dataclass(frozen=True)
class IpsecIdentity:
    """The IPsec instance of a server and the IP its server certificate is issued for.

    No name is looked up in a request: for a server given by a name the panel cannot tell
    whether the certificate still fits, so it only offers the reissue.
    """

    instance: ProtocolInstance

    @classmethod
    def of(cls, server: Server) -> "IpsecIdentity | None":
        instance = ProtocolInstance.objects.filter(
            server=server, container=IPSEC_CONTAINER, container_removed_at__isnull=True
        ).first()
        return cls(instance) if instance is not None else None

    @property
    def issued_for(self) -> str:
        return str(self.instance.params.get("server_ip", ""))

    @property
    def server_host(self) -> str:
        return self.instance.server.host

    @property
    def outdated(self) -> bool:
        """The server address is an IP other than the one of the certificate."""
        return is_ipv4(self.server_host) and self.server_host != self.issued_for


def start_reissue(
    instance: ProtocolInstance, user: User, *, reissue: bool, ip: str | None = None
) -> Operation:
    """`reissue`: imported keys, which have no profile to update, are issued anew."""
    params = {"instance_id": instance.pk, "user_id": user.pk, "reissue": reissue}
    operation = launch(REISSUE_KIND, params, user=user, subject=str(instance))
    data = {"operation": operation.pk, "host": instance.server.host}
    audit.log(REISSUE_KIND, actor=user, target=instance, data=data, ip=ip)
    return operation


class ServerCertificate:
    """The server certificate in amnezia-ipsec, changed under the server lock (DESIGN.md 5.4).

    The new certificate is made before ikev2.conf names it, and the old one is deleted only
    once pluto runs with the new one, so a failed step leaves a server that still starts.
    """

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._host = host
        self._ctx = ctx
        self._ipsec = IpsecDocker(host, ctx)

    def reissue(self, server_host: str) -> str:
        """Issue the certificate for the IP of `server_host`; returns that IP.

        Run again after a failure, it finds the finished steps done and goes on.
        """
        server_ip = server_host if is_ipv4(server_host) else ""
        server_ip = server_ip or resolve_server_ip(self._ctx, server_host)
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Read the server certificate")):
                previous = self._ipsec.server_ip()
                self._ctx.log(_("Issued for %(ip)s.") % {"ip": previous})
            if previous != server_ip:
                self._replace(previous, server_ip)
            else:
                with self._ctx.step(_("Wait for IPsec to start")):
                    self._ipsec.ensure_running()
        return server_ip

    def _replace(self, previous: str, server_ip: str) -> None:
        shell = self._ipsec.shell
        with self._ctx.step(_("Issue the server certificate")):
            if not self._has(server_ip):
                self._nss(_sign_script(server_ip))
        with self._ctx.step(_("Write the protocol configuration")):
            text = shell.read_file(IKEV2_CONFIG_PATH).decode()
            shell.write_file_atomic(
                IKEV2_CONFIG_PATH, with_server_identity(text, server_ip).encode(), mode=0o644
            )
        with self._ctx.step(_("Restart IPsec")):
            self._host.docker("restart", IPSEC_CONTAINER)
            self._ipsec.ensure_running()
        with self._ctx.step(_("Delete the old server certificate")):
            if self._has(previous):
                self._nss(shlex.join(["certutil", "-F", *_NSS, "-n", previous]))

    def _has(self, nickname: str) -> bool:
        command = shlex.join(["certutil", "-L", *_NSS, "-n", nickname])
        return self._ipsec.shell.sh(f"{command} >/dev/null").ok

    def _nss(self, script: str) -> None:
        result = self._ipsec.shell.sh(script)
        if not result.ok:
            raise SetupError(SetupErrorCode.IPSEC_CERTIFICATE_FAILED, f"exit {result.exit_code}")


class IpsecProfiles:
    """The panel's side: the instance and the profiles of its keys name the new server IP."""

    def __init__(self, instance: ProtocolInstance, ctx: OperationContext) -> None:
        self._instance = instance
        self._ctx = ctx

    @transaction.atomic
    def save(self, server_ip: str) -> list[AccessKey]:
        """Store the IP and update the profiles; returns the keys whose owners must know."""
        instance = self._instance
        instance.params = {**instance.params, "server_ip": server_ip}
        instance.save(update_fields=["params"])
        keys = []
        for key in self._issued_keys():
            profile = IpsecKeyProfile.from_json(key.profile)
            moved = replace(profile, host=instance.server.host, server_ip=server_ip)
            if moved != profile:
                key.profile = moved.to_json()
                key.save(update_fields=["profile"])
                keys.append(key)
        self._ctx.log(_("Key profiles updated: %(count)d.") % {"count": len(keys)})
        return keys

    def _issued_keys(self) -> list[AccessKey]:
        """Keys with a profile, the rows locked: a key being issued waits for the new IP.

        A key still being issued (no profile yet) is locked too, so it is saved after this
        transaction and reads the new IP then (`IssuedKeys`), or before it and is found here.
        """
        keys = (
            AccessKey.objects.select_for_update(of=("self",))
            .filter(source=self._instance.source)
            .exclude(status=KeyStatus.REVOKED)
            .select_related("user")
        )
        return [key for key in keys if key.profile]


def _sign_script(server_ip: str) -> str:
    """The certutil command of configure_container.sh; the noise goes through a file."""
    options = shlex.join(server_certificate_options(server_ip))
    return (
        'noise=$(mktemp) && head -c 1024 /dev/urandom > "$noise" && '
        f'certutil -z "$noise" {options} </dev/null; code=$?; rm -f "$noise"; exit $code'
    )
