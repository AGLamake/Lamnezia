"""How a key operation reaches the server: WireGuard-family peers, XRay, OpenVPN or IPsec clients.

Operations work with `KeyBackend` and do not care which protocol an instance runs.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from ipaddress import IPv4Address
from typing import Protocol

from django.utils import timezone

from amnezia_core.awg import Peer
from amnezia_core.awg_export import AwgServerSettings
from amnezia_core.clients_table import ClientRecord
from amnezia_core.containers import WgContainer
from amnezia_core.ipsec import IpsecContainer
from amnezia_core.ipsec import new_client_id as new_ipsec_client_id
from amnezia_core.keys import KeyPair
from amnezia_core.openvpn import ClientKey as OpenVpnClientKey
from amnezia_core.openvpn import OpenVpnContainer, new_client_id
from amnezia_core.xray import client_fingerprint
from apps.keys.models import DISABLED_STATUSES, AccessKey, KeyStatus
from apps.keys.services.instances import Instance
from apps.keys.services.ipsec_clients import IpsecClientManager, IssuedIpsecClient
from apps.keys.services.openvpn_clients import ClientFiles, OpenVpnClientManager
from apps.keys.services.peers import PeerManager, StepRecorder
from apps.keys.services.profile import (
    IpsecKeyProfile,
    OpenVpnKeyProfile,
    ServerProfile,
)
from apps.keys.services.swaps import Swap, SwapResult
from apps.keys.services.xray_clients import XrayClientManager
from apps.keys.services.xray_profiles import XrayProfiles
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class Reissue:
    """An old key and the new one issued in its place; an old key revoked meanwhile stays so."""

    old: AccessKey
    new: AccessKey

    def swap(self, client_id: Callable[[AccessKey], str]) -> Swap:
        record = ClientRecord(self.new.name, timezone.localtime(), self.new.user_id)
        old = "" if self.old.status == KeyStatus.REVOKED else client_id(self.old)
        return Swap(client_id(self.new), record, old)


@dataclass
class ReissueOutcome:
    """The pairs whose new key is on the server, profile filled, and those that failed, why."""

    done: list[Reissue] = field(default_factory=list)
    failed: list[tuple[Reissue, str]] = field(default_factory=list)

    @classmethod
    def of[T](
        cls,
        pairs: Sequence[Reissue],
        swaps: Sequence[Swap],
        result: SwapResult[T],
        fill: Callable[[AccessKey, T], None],
    ) -> "ReissueOutcome":
        outcome = cls()
        for pair, swap in zip(pairs, swaps, strict=True):
            if swap.new in result.done:
                fill(pair.new, result.done[swap.new])
                outcome.done.append(pair)
            else:
                outcome.failed.append((pair, result.failed.get(swap.new, "")))
        return outcome


class KeyBackend(Protocol):
    """Changes of one key on the server; each call is idempotent."""

    def issue(self, key: AccessKey) -> None:
        """Add the client and fill `client_ip`, `profile` and, for IPsec, `private_key`.

        The key is not saved.
        """
        ...

    def disable(self, key: AccessKey) -> None: ...

    def enable(self, key: AccessKey) -> None: ...

    def revoke(self, key: AccessKey) -> None: ...

    def rename(self, key: AccessKey) -> None: ...

    def assign(self, key: AccessKey) -> None:
        """Write the owner of an active key to clientsTable."""
        ...

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        """Issue the new keys and revoke the old ones in one change of the server files.

        The new keys get `client_ip`, `profile` and, for IPsec, `private_key`; nothing is saved.
        """
        ...


def new_credentials(instance: Instance) -> tuple[str, str]:
    """(public_key, private_key) of a new key of the instance.

    An XRay client is a UUID, its credential: the key keeps it as the encrypted private key and
    is found by its fingerprint. An OpenVPN client is the common name of its certificate; the
    panel makes its RSA key, which never leaves the panel but in the user's profile. An IPsec
    client is the nickname of its certificate; the key and the certificate are made in the
    container, so the private key comes with the issue.
    """
    if isinstance(instance.container, WgContainer):
        pair = KeyPair.generate()
        return pair.public_key, pair.private_key
    if isinstance(instance.container, OpenVpnContainer):
        return new_client_id(), OpenVpnClientKey.generate().pem
    if isinstance(instance.container, IpsecContainer):
        return new_ipsec_client_id(), ""
    client_id = str(uuid.uuid4())
    return client_fingerprint(client_id), client_id


def backend_for(instance: Instance, host: RemoteHost, steps: StepRecorder) -> KeyBackend:
    if isinstance(instance.container, WgContainer):
        kind = instance.container
        return WgKeys(instance, kind, PeerManager(host, steps, kind))
    if isinstance(instance.container, OpenVpnContainer):
        return OpenVpnKeys(instance, OpenVpnClientManager(host, steps))
    if isinstance(instance.container, IpsecContainer):
        return IpsecKeys(instance, IpsecClientManager(host, steps))
    clients = XrayClientManager(host, steps, instance.source)
    return XrayKeys(clients, XrayProfiles(instance, host))


class WgKeys:
    """A key is a peer, found by its public key."""

    def __init__(self, instance: Instance, kind: WgContainer, peers: PeerManager) -> None:
        self._instance = instance
        self._kind = kind
        self.peers = peers

    def issue(self, key: AccessKey) -> None:
        peer = self.peers.add(
            key.public_key, key.name, user_id=key.user_id, reserved=self._reserved(key.source)
        )
        self._fill(key, peer, *self.peers.server_settings())

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        swaps = [pair.swap(_public_key) for pair in pairs]
        old_keys = [pair.old.pk for pair in pairs]
        return self._swap(pairs, swaps, self._reserved(self._instance.source, old_keys))

    def move(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        """Issue the new keys of `pairs` here in one change; the old ones stay where they are."""
        swaps = [replace(pair.swap(_public_key), old="") for pair in pairs]
        return self._swap(pairs, swaps, self._reserved(self._instance.source))

    def _swap(
        self, pairs: Sequence[Reissue], swaps: Sequence[Swap], reserved: Sequence[IPv4Address]
    ) -> ReissueOutcome:
        result = self.peers.replace(swaps, reserved=reserved)
        server = self.peers.server_settings() if result.done else None

        def fill(key: AccessKey, peer: Peer) -> None:
            assert server is not None
            self._fill(key, peer, *server)

        return ReissueOutcome.of(pairs, swaps, result, fill)

    @staticmethod
    def _reserved(source: str, exclude: Sequence[int] = ()) -> list[IPv4Address]:
        """Disabled keys keep their addresses to come back with them."""
        addresses = (
            AccessKey.objects.filter(source=source, status__in=DISABLED_STATUSES)
            .exclude(client_ip=None)
            .exclude(pk__in=exclude)
            .values_list("client_ip", flat=True)
        )
        return [IPv4Address(ip) for ip in addresses]

    def _fill(
        self, key: AccessKey, peer: Peer, server: AwgServerSettings, server_public_key: str
    ) -> None:
        profile = ServerProfile.build(
            self._kind, self._instance.host, server, server_public_key, peer.preshared_key or ""
        )
        key.client_ip = str(peer.addresses[0])
        key.profile = replace(profile, dns=self._instance.dns).to_json()

    def disable(self, key: AccessKey) -> None:
        self.peers.disable(key.public_key)

    def enable(self, key: AccessKey) -> None:
        """Add the peer of the key with its stored address and preshared key."""
        preshared_key = (
            ServerProfile.from_json(key.profile).preshared_key
            if key.profile
            else self.peers.preshared_key()
        )
        peer = Peer(key.public_key, (f"{key.client_ip}/32",), preshared_key)
        self.peers.enable(peer, key.name, user_id=key.user_id)

    def revoke(self, key: AccessKey) -> None:
        self.peers.revoke(key.public_key)

    def rename(self, key: AccessKey) -> None:
        self.peers.rename(key.public_key, key.name)

    def assign(self, key: AccessKey) -> None:
        self.peers.add(key.public_key, key.name, user_id=key.user_id)


class XrayKeys:
    """A key is a client of server.json, found by its UUID kept as the private key.

    Every change of server.json restarts the container, as in the Amnezia app.
    """

    def __init__(self, clients: XrayClientManager, profiles: XrayProfiles) -> None:
        self._clients = clients
        self._profiles = profiles

    def issue(self, key: AccessKey) -> None:
        self._clients.add(key.private_key, key.name, user_id=key.user_id)
        key.profile = self._profiles.current().to_json()

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        swaps = [pair.swap(_uuid) for pair in pairs]
        result = self._clients.replace(swaps)
        profile = self._profiles.current().to_json() if result.done else ""

        def fill(key: AccessKey, _: None) -> None:
            key.profile = profile

        return ReissueOutcome.of(pairs, swaps, result, fill)

    def disable(self, key: AccessKey) -> None:
        self._clients.disable(key.private_key)

    def enable(self, key: AccessKey) -> None:
        self._clients.enable(key.private_key, key.name, user_id=key.user_id)

    def revoke(self, key: AccessKey) -> None:
        self._clients.revoke(key.private_key)

    def rename(self, key: AccessKey) -> None:
        self._clients.rename(key.private_key, key.name)

    def assign(self, key: AccessKey) -> None:
        self._clients.add(key.private_key, key.name, user_id=key.user_id)


class OpenVpnKeys:
    """A key is a client certificate, found by its common name kept as the public key.

    Every change of the revocation list restarts openvpn, so all its clients reconnect.
    """

    def __init__(self, instance: Instance, clients: OpenVpnClientManager) -> None:
        self._instance = instance
        self._clients = clients

    def issue(self, key: AccessKey) -> None:
        files = self._clients.issue(
            key.public_key, OpenVpnClientKey(key.private_key), key.name, user_id=key.user_id
        )
        self._fill(key, files)

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        swaps = [pair.swap(_public_key) for pair in pairs]
        keys = {pair.new.public_key: OpenVpnClientKey(pair.new.private_key) for pair in pairs}
        result = self._clients.replace(swaps, keys)
        return ReissueOutcome.of(pairs, swaps, result, self._fill)

    def _fill(self, key: AccessKey, files: ClientFiles) -> None:
        """server.conf gives the settings; the extra client config and the DNS block are the
        panel's own, as the app keeps them on the device.
        """
        instance = self._instance
        settings = replace(files.settings, additional_client_config=instance.openvpn_client_config)
        profile = OpenVpnKeyProfile.build(
            instance.host, settings, files.ca_cert, files.client_cert, files.ta_key
        )
        profile = replace(profile, dns=instance.dns, block_outside_dns=instance.block_outside_dns)
        key.profile = profile.to_json()

    def disable(self, key: AccessKey) -> None:
        self._clients.disable(key.public_key)

    def enable(self, key: AccessKey) -> None:
        self._clients.enable(key.public_key, key.name, user_id=key.user_id)

    def revoke(self, key: AccessKey) -> None:
        self._clients.revoke(key.public_key)

    def rename(self, key: AccessKey) -> None:
        self._clients.rename(key.public_key, key.name)

    def assign(self, key: AccessKey) -> None:
        self._clients.assign(key.public_key, key.name, user_id=key.user_id)


class IpsecKeys:
    """A key is a client certificate made in the container, found by its nickname.

    The PKCS#12 with the certificate and its key becomes the key's private key.
    """

    def __init__(self, instance: Instance, clients: IpsecClientManager) -> None:
        self._instance = instance
        self._clients = clients

    def issue(self, key: AccessKey) -> None:
        self._fill(key, self._clients.issue(key.public_key, key.name, user_id=key.user_id))

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        swaps = [pair.swap(_public_key) for pair in pairs]
        result = self._clients.replace(swaps)
        return ReissueOutcome.of(pairs, swaps, result, self._fill)

    def _fill(self, key: AccessKey, issued: IssuedIpsecClient) -> None:
        key.private_key = issued.p12
        profile = IpsecKeyProfile(self._instance.host, issued.server_ip, issued.ca_cert)
        key.profile = replace(profile, dns=self._instance.dns).to_json()

    def disable(self, key: AccessKey) -> None:
        self._clients.disable(key.public_key)

    def enable(self, key: AccessKey) -> None:
        self._clients.enable(key.public_key, key.name, user_id=key.user_id)

    def revoke(self, key: AccessKey) -> None:
        self._clients.revoke(key.public_key)

    def rename(self, key: AccessKey) -> None:
        self._clients.rename(key.public_key, key.name)

    def assign(self, key: AccessKey) -> None:
        self._clients.assign(key.public_key, key.name, user_id=key.user_id)


def _public_key(key: AccessKey) -> str:
    """How WireGuard-family, OpenVPN and IPsec clients are known on the server."""
    return key.public_key


def _uuid(key: AccessKey) -> str:
    """An XRay client is known by its UUID, kept as the private key."""
    return str(key.private_key)
