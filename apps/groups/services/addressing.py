"""Where keys of a server group send clients, the DNS check of its domain, and moving keys there.

docs/FALLBACK.md section 6. Once the relay of a group is installed, its keys carry the domain of
the group, or the relay address, and the port of the group: the Amnezia app resolves the name
on every connection. Keys the leading server had before keep its own address in their profiles;
they are moved to the relay and their owners are told to import them again, once.
"""

import json
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext as _

from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.keys.models import GONE_STATUSES, AccessKey
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.installer import ipv4_addresses


def _relayed() -> QuerySet[ServerGroup]:
    """Groups whose relay is installed: only then do clients reach them through it."""
    return ServerGroup.objects.filter(relay__relay_role__installed_at__isnull=False).select_related(
        "relay"
    )


def _host(group: ServerGroup) -> str:
    assert group.relay is not None
    return group.domain or group.relay.host


def client_host(group: ServerGroup) -> str | None:
    """The address keys of the group give clients; None while the group has no relay running."""
    relayed = _relayed().filter(pk=group.pk).first()
    return None if relayed is None else _host(relayed)


def client_hosts() -> dict[str, str]:
    """The source of every leading server behind a running relay -> the address of its group."""
    hosts = {group.pk: _host(group) for group in _relayed()}
    leaders = GroupMember.objects.filter(leading=True, group_id__in=hosts).values_list(
        "group_id", "instance_id"
    )
    return {ProtocolInstance.source_for(instance): hosts[group] for group, instance in leaders}


def key_sources() -> dict[str, str]:
    """The source of every member that runs the group identity -> the source of its leader.

    Clients of the group come to members with keys of the leading server, so their traffic
    counts for those keys.
    """
    members = GroupMember.objects.exclude(state=MemberState.JOINING).values_list(
        "group_id", "instance_id", "leading"
    )
    leaders = {group: instance for group, instance, leading in members if leading}
    return {
        ProtocolInstance.source_for(instance): ProtocolInstance.source_for(leaders[group])
        for group, instance, leading in members
        if not leading and group in leaders
    }


def tell_owners(keys: Iterable[AccessKey], server: str) -> int:
    """Tell every owner of `keys` once that their keys of `server` must be imported again."""
    owners = {key.user for key in keys if key.user is not None}
    for owner in owners:
        emit(Event.KEYS_UPDATED, {"user": owner.username, "server": server}, user=owner)
    return len(owners)


class GroupKeyAddress:
    """Keys of the leading server whose profiles do not send clients to the relay yet.

    The old address goes on working while the leading server is up, so nothing changes on the
    server: the profile gets the new address, and the owner imports the key again.
    """

    def __init__(self, group: ServerGroup) -> None:
        self.group = group
        self._leader = group.leader
        self._host = client_host(group)
        self.outdated: list[AccessKey] = []
        if self._leader is not None and self._host is not None:
            keys = (
                AccessKey.objects.filter(source=self._leader.instance.source)
                .exclude(status__in=GONE_STATUSES)
                .exclude(profile="")
                .select_related("user")
            )
            self.outdated = [key for key in keys if _endpoint(key) != (self._host, group.port)]

    @transaction.atomic
    def apply(self) -> int:
        """Write the address of the group into the outdated profiles; the number of keys moved."""
        if self._leader is None or self._host is None:
            return 0
        for key in self.outdated:
            key.profile = _with_endpoint(key.profile, self._host, self.group.port)
            key.save(update_fields=["profile"])
        tell_owners(self.outdated, self._leader.instance.server.name)
        return len(self.outdated)


def _endpoint(key: AccessKey) -> tuple[str, int]:
    profile = json.loads(key.profile)
    return str(profile.get("host", "")), int(profile.get("port", 0))


def _with_endpoint(profile: str, host: str, port: int) -> str:
    """A WireGuard key profile keeps the address in `host` and `port`, as sorted JSON."""
    return json.dumps({**json.loads(profile), "host": host, "port": port}, sort_keys=True)


def _ipv6_addresses(name: str) -> list[str]:
    try:
        found = socket.getaddrinfo(name, None, socket.AF_INET6)
    except UnicodeError as error:
        raise OSError(str(error)) from error
    return list(dict.fromkeys(str(entry[4][0]) for entry in found))


@dataclass(frozen=True)
class DomainCheck:
    """Whether the domain of a group leads clients to its relay, as warnings for its page.

    The domain needs only an A record with the relay address: the desktop app takes the first
    address of the answer, and with an AAAA record it may go to IPv6 past the relay.
    """

    group: ServerGroup
    resolve: Callable[[str], list[str]] = ipv4_addresses
    resolve_ipv6: Callable[[str], list[str]] = _ipv6_addresses

    def warnings(self) -> list[str]:
        group = self.group
        if not group.domain or group.relay is None:
            return []
        domain, relay = group.domain, group.relay.host
        warnings = []
        found = self._lookup(self.resolve, domain)
        expected = self._lookup(self.resolve, relay)
        if not found:
            warnings.append(
                _(
                    "%(domain)s does not resolve: keys of the group do not connect until its "
                    "A record points to the relay %(relay)s."
                )
                % {"domain": domain, "relay": relay}
            )
        elif expected and set(found) != set(expected):
            warnings.append(
                _("%(domain)s points to %(found)s, not to the relay %(relay)s (%(expected)s).")
                % {
                    "domain": domain,
                    "found": ", ".join(found),
                    "relay": relay,
                    "expected": ", ".join(expected),
                }
            )
        if self._lookup(self.resolve_ipv6, domain):
            warnings.append(
                _(
                    "%(domain)s has an AAAA record. The Amnezia app on a computer may take the "
                    "IPv6 address and miss the relay: keep only the A record."
                )
                % {"domain": domain}
            )
        return warnings

    @staticmethod
    def _lookup(resolve: Callable[[str], list[str]], name: str) -> list[str]:
        try:
            return resolve(name)
        except OSError:
            return []
