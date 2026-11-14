"""Relay monitoring: the agent state read over SSH, kept with the relay, and its notifications.

docs/FALLBACK.md section 7. A reading matches the members the agent names by address to the
group members and notifies of a switch, a member the relay cannot reach and a group with no
member left. A relay whose state the panel cannot read for two intervals is reported once per
outage, as a server that is down. Members that lag behind their group too long are reported too.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from ipaddress import IPv4Address

from django.utils import timezone

from amnezia_core.errors import AmneziaFormatError
from amnezia_core.relay import Backend, RelayState
from apps.groups.models import GroupMember, ServerGroup
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.protocols.services.installer import ipv4_addresses
from apps.relays.models import Relay
from apps.relays.services.container import FOLDER, NAME
from apps.relays.services.health import AllDown, Change, Member, MemberDown, RelayHealth, Switched
from apps.servers.models import Server
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError
from apps.ssh.services import RemoteHost

logger = logging.getLogger(__name__)

Connect = Callable[[Server], AbstractContextManager[RemoteHost]]

STATE_PATH = f"{FOLDER}/state.json"
BAD_STATE = "bad_state"
STALE = "stale"

_READING_FIELDS = ("health", "read_at", "read_error", "rate_from_clients", "rate_to_clients")


class StateUnreadableError(Exception):
    """The relay answered, but its state is not one to trust: `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RelayMonitor:
    """Reads the agent state of one relay; `interval` is how often the beat asks."""

    def __init__(
        self,
        interval: timedelta,
        connect: Connect = connect,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._interval = interval
        self._connect = connect
        self._clock = clock

    def poll(self, relay: Relay) -> None:
        try:
            state = self._read(relay.server)
        except SshError as error:
            self._failed(relay, str(error.code))
        except StateUnreadableError as error:
            self._failed(relay, error.code)
        else:
            self._record(relay, state)

    def _read(self, server: Server) -> RelayState:
        with self._connect(server) as host:
            text = host.container(NAME).read_file(STATE_PATH)
        try:
            state = RelayState.parse(text.decode("utf-8"))
        except (AmneziaFormatError, UnicodeDecodeError) as error:
            raise StateUnreadableError(BAD_STATE) from error
        # The agent writes every few seconds: an old file means it stopped.
        if self._clock() - state.updated_at > 2 * self._interval:
            raise StateUnreadableError(STALE)
        return state

    def _record(self, relay: Relay, state: RelayState) -> None:
        previous = RelayHealth.load(relay.health)
        health = RelayHealth.of(state, _members(relay.server))
        rates = health.rates(previous)
        relay.health = health.to_dict()
        relay.read_at, relay.read_error = self._clock(), ""
        relay.rate_from_clients, relay.rate_to_clients = rates or (None, None)
        relay.save(update_fields=[*_READING_FIELDS])
        groups = {group.port: group for group in ServerGroup.objects.filter(relay=relay.server)}
        for change in health.changes(previous):
            group = groups.get(change.port)
            if group is not None:
                _notify(change, group, relay, health.updated_at)

    def _failed(self, relay: Relay, code: str) -> None:
        logger.warning("relay %s state not read: %s", relay.server.name, code)
        relay.read_error = code
        relay.save(update_fields=["read_error"])
        last = relay.read_at or relay.installed_at
        if last is not None and self._clock() - last > 2 * self._interval:
            _report_down(relay)


def _members(server: Server) -> dict[int, dict[Backend, Member]]:
    """The members of each group of the relay by every address their server name gives."""
    found: dict[int, dict[Backend, Member]] = {}
    members = GroupMember.objects.filter(group__relay=server).select_related(
        "group", "instance__server"
    )
    for member in members:
        server_of = member.instance.server
        try:
            addresses = ipv4_addresses(server_of.host)
        except OSError:
            continue
        known = Member(member.pk, server_of.name)
        for address in addresses:
            backend = Backend(IPv4Address(address), member.instance.port)
            found.setdefault(member.group.port, {})[backend] = known
    return found


def _notify(change: Change, group: ServerGroup, relay: Relay, at: int) -> None:
    """`at`: when the agent saw it, which names the occurrence."""
    relay_name = relay.server.name
    match change:
        case Switched(before=before, after=after, at=switched_at):
            emit(
                Event.RELAY_SWITCHED,
                {"group": group.name, "relay": relay_name, "from": before.name, "to": after.name},
                key=f"relay_switched:{group.pk}:{switched_at}",
            )
        case MemberDown(member=member):
            emit(
                Event.MEMBER_UNREACHABLE,
                {"group": group.name, "relay": relay_name, "server": member.name},
                key=f"member_unreachable:{member.id}:{at}",
            )
        case AllDown():
            emit(
                Event.GROUP_UNREACHABLE,
                {"group": group.name, "relay": relay_name},
                key=f"group_unreachable:{group.pk}:{at}",
            )


def _report_down(relay: Relay) -> None:
    """Once per outage: the key holds the last reading, which changes only after a new one."""
    last_seen = relay.read_at.isoformat() if relay.read_at else ""
    emit(
        Event.RELAY_DOWN,
        {"relay": relay.server.name, "host": relay.server.host, "last_seen": last_seen},
        key=f"relay_down:{relay.pk}:{last_seen or 'never'}",
    )


def report_lagging(threshold: timedelta, now: datetime | None = None) -> None:
    """Members that lag behind their group longer than `threshold`, once per lag."""
    since = (now or timezone.now()) - threshold
    lagging = GroupMember.objects.filter(lagging_since__lte=since).select_related(
        "group", "instance__server"
    )
    for member in lagging:
        assert member.lagging_since is not None
        moment = member.lagging_since.isoformat()
        emit(
            Event.MEMBER_LAGGING,
            {"group": member.group.name, "server": member.instance.server.name, "since": moment},
            key=f"member_lagging:{member.pk}:{moment}",
        )
