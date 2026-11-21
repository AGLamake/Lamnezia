"""Where keys of a server group send clients, the DNS check of its domain, and moving keys there.

docs/FALLBACK.md section 6. Once the relay of a group is installed, its keys carry the domain of
the group, or the relay address, and the port of the group: the Amnezia app resolves the name
on every connection. Keys the leading server had before keep its own address in their profiles;
they are moved to the relay and their owners are told to import them again, once. Without the
relay the keys move back to the leading server the same way.
"""

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass

from django.core.cache import cache
from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext as _

from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.keys.models import GONE_STATUSES, AccessKey
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_identity import tell_owners
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


class GroupKeyAddress:
    """Keys of the leading server whose profiles do not send clients where the group does now.

    That is the address of the group behind its running relay, and the leading server without
    one; `host` replaces it. The old address goes on working while it is up, so nothing changes
    on the server: the profile gets the new address, and the owner imports the key again.

    A key may be issued while the address changes, e.g. while the relay is installed. `apply`
    locks the key rows of the leading server, as saving an issued key does (keys/services/
    issued.py), and only then finds the outdated ones: a key saved before is found and moved,
    and a key saved after takes the new address itself.
    """

    def __init__(self, group: ServerGroup, host: str | None = None) -> None:
        self.group = group
        self._leader = group.leader
        self._host = host or client_host(group)
        if self._host is None and self._leader is not None:
            self._host = self._leader.instance.server.host

    @property
    def outdated(self) -> list[AccessKey]:
        """The outdated keys as they are now, without locking them."""
        return self._outdated(lock=False)

    @transaction.atomic
    def apply(self) -> int:
        """Write the address of the group into the outdated profiles; the number of keys moved."""
        if self._leader is None or self._host is None:
            return 0
        outdated = self._outdated(lock=True)
        for key in outdated:
            key.profile = _with_endpoint(key.profile, self._host, self.group.port)
            key.save(update_fields=["profile"])
        tell_owners(outdated, self._leader.instance.server.name)
        return len(outdated)

    def _outdated(self, *, lock: bool) -> list[AccessKey]:
        if self._leader is None or self._host is None:
            return []
        keys = AccessKey.objects.filter(source=self._leader.instance.source).exclude(
            status__in=GONE_STATUSES
        )
        if lock:
            keys = keys.select_for_update(of=("self",))
        # The profile is encrypted, so an empty one is only seen after it is read: an imported
        # key has none and keeps working through the old address.
        endpoint = (self._host, self.group.port)
        return [
            key for key in keys.select_related("user") if key.profile and _endpoint(key) != endpoint
        ]


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
class DomainAnswers:
    """What DNS answered about the domain of a group and its relay."""

    found: list[str]
    expected: list[str]
    ipv6: list[str]


@dataclass(frozen=True)
class DomainCheck:
    """Whether the domain of a group leads clients to its relay, as warnings for its page.

    The domain needs only an A record with the relay address: the desktop app takes the first
    address of the answer, and with an AAAA record it may go to IPv6 past the relay. DNS is
    asked in the background (`run`); the page reads the cached answers, so it never waits for
    DNS. Another domain or relay is another cache entry, checked anew.
    """

    group: ServerGroup
    resolve: Callable[[str], list[str]] = ipv4_addresses
    resolve_ipv6: Callable[[str], list[str]] = _ipv6_addresses

    # A changed DNS record shows on the page within this time.
    TTL = 10 * 60

    @property
    def needed(self) -> bool:
        """Only a group with a domain and a relay has a domain to check."""
        return bool(self.group.domain) and self.group.relay is not None

    @property
    def _key(self) -> str:
        assert self.group.relay is not None
        return f"groups:domain-check:{self.group.pk}:{self.group.domain}:{self.group.relay.host}"

    def run(self) -> None:
        """Ask DNS about the domain and the relay and keep the answers for the page."""
        if not self.needed:
            return
        assert self.group.relay is not None
        domain = self.group.domain
        answers = DomainAnswers(
            found=self._lookup(self.resolve, domain),
            expected=self._lookup(self.resolve, self.group.relay.host),
            ipv6=self._lookup(self.resolve_ipv6, domain),
        )
        cache.set(self._key, answers, self.TTL)

    def answers(self) -> DomainAnswers | None:
        """The cached answers; None while the domain has not been checked yet."""
        return cache.get(self._key) if self.needed else None

    def warnings(self) -> list[str]:
        """Warnings from the cached answers; none before the check has run."""
        answers = self.answers()
        if answers is None:
            return []
        assert self.group.relay is not None
        domain, relay = self.group.domain, self.group.relay.host
        found, expected = answers.found, answers.expected
        warnings = []
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
        if answers.ipv6:
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
