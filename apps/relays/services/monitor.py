"""Relay monitoring: the agent state read over SSH, kept with the relay, and its notifications.

docs/FALLBACK.md section 7. A reading matches the members the agent names by address to the
group members and notifies of a switch, a member the relay cannot reach and a group with no
member left. A relay whose state the panel cannot read for two intervals is reported once per
outage, as a server that is down. Members that lag behind their group too long are reported too.
Each of these but a switch has a pair told once when it is over; an outage and a lag are
remembered apart from notifications (notifications/services/trouble.py), so the pair comes even
when nobody hears of the trouble itself.
Every reading with a known load is kept for the load chart (services/load.py).
A reading is also compared with the groups the panel wrote (services/mismatch.py): a new
mismatch drops the synced digest, so the next sync writes the relay again.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from ipaddress import IPv4Address

from django.utils import timezone

from amnezia_core.errors import AmneziaFormatError
from amnezia_core.relay import Backend, RelayState
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.notifications.services.trouble import (
    forget_trouble,
    open_troubles,
    report_recovery,
    report_trouble,
)
from apps.protocols.services.installer import ipv4_addresses
from apps.relays.models import Relay, RelaySample
from apps.relays.services.config import RelayPlan
from apps.relays.services.container import FOLDER, NAME
from apps.relays.services.health import (
    AllDown,
    Change,
    GroupUp,
    Member,
    MemberDown,
    MemberUp,
    RelayHealth,
    Switched,
)
from apps.relays.services.mismatch import RelayMismatch
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
        _report_up(relay)
        health = RelayHealth.of(state, _members(relay.server))
        rates = health.rates(previous)
        relay.health = health.to_dict()
        relay.read_at, relay.read_error = self._clock(), ""
        relay.rate_from_clients, relay.rate_to_clients = rates or (None, None)
        relay.save(update_fields=[*_READING_FIELDS, *_compare(relay, state)])
        if rates is not None:
            RelaySample.objects.create(
                relay=relay, at=relay.read_at, rate_from_clients=rates[0], rate_to_clients=rates[1]
            )
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


def _compare(relay: Relay, state: RelayState) -> list[str]:
    """Set the mismatch of the reading on `relay` and return the fields it changed.

    A mismatch the relay did not have drops `synced`: the next sync updates the relay, and the
    update starts the container again when a port of a group is not published. The same mismatch
    after that update is left to the card, so a relay that keeps it is not updated every minute.
    The kept mismatch stays while the state may lag behind the panel: an update is due, or the
    agent wrote the state before the last update.
    """
    plan = RelayPlan.of(relay.server)
    try:
        config = plan.config()
    except OSError:
        return []
    written = relay.synced_at
    if plan.digest(config) != relay.synced or (written and state.updated_at < written):
        return []
    found = RelayMismatch.of(config, state)
    changed = ["mismatch"]
    if found and found != relay.settings_mismatch:
        logger.warning("relay %s forwards other groups than the panel wrote", relay.server.name)
        relay.synced = ""
        changed.append("synced")
    relay.mismatch = found.to_dict()
    return changed


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
        case MemberUp(member=member):
            emit(
                Event.MEMBER_REACHABLE,
                {"group": group.name, "relay": relay_name, "server": member.name},
                key=f"member_reachable:{member.id}:{at}",
            )
        case GroupUp():
            emit(
                Event.GROUP_REACHABLE,
                {"group": group.name, "relay": relay_name},
                key=f"group_reachable:{group.pk}:{at}",
            )


def _report_down(relay: Relay) -> None:
    """Once per outage: the key holds the last reading, which changes only after a new one."""
    last_seen = _last_seen(relay)
    report_trouble(
        Event.RELAY_DOWN,
        {"relay": relay.server.name, "host": relay.server.host, "last_seen": last_seen},
        key=f"relay_down:{relay.pk}:{last_seen or 'never'}",
    )


def _report_up(relay: Relay) -> None:
    """Before a reading is kept: the outage with the same last reading ends."""
    last_seen = _last_seen(relay)
    occurrence = f"{relay.pk}:{last_seen or 'never'}"
    report_recovery(
        f"relay_down:{occurrence}",
        Event.RELAY_UP,
        {"relay": relay.server.name, "host": relay.server.host, "last_seen": last_seen},
        key=f"relay_up:{occurrence}",
    )


def _last_seen(relay: Relay) -> str:
    return relay.read_at.isoformat() if relay.read_at else ""


def report_lagging(threshold: timedelta, now: datetime | None = None) -> None:
    """Members that lag behind their group longer than `threshold`, once per lag."""
    since = (now or timezone.now()) - threshold
    lagging = GroupMember.objects.filter(lagging_since__lte=since).select_related(
        "group", "instance__server"
    )
    for member in lagging:
        assert member.lagging_since is not None
        moment = member.lagging_since.isoformat()
        data = {
            "group": member.group.name,
            "server": member.instance.server.name,
            "since": moment,
        }
        if member.lagging_reason:  # a member lagging since an older release has none
            data["reason"] = member.lagging_reason
        report_trouble(Event.MEMBER_LAGGING, data, key=f"member_lagging:{member.pk}:{moment}")


def report_caught_up() -> None:
    """Members found lagging that no longer lag since that moment, once per lag.

    The key of a lag names the member and the moment it started: `member_lagging:<id>:<moment>`.
    """
    lagging, caught_up = f"{Event.MEMBER_LAGGING}:", f"{Event.MEMBER_CAUGHT_UP}:"
    for key in open_troubles(lagging):
        member_id, moment = key.removeprefix(lagging).split(":", 1)
        member = (
            GroupMember.objects.filter(pk=member_id)
            .select_related("group", "instance__server")
            .first()
        )
        if member is None:
            forget_trouble(key)
            continue
        if member.state != MemberState.MEMBER or _lags_since(member, moment):
            continue
        report_recovery(
            key,
            Event.MEMBER_CAUGHT_UP,
            {"group": member.group.name, "server": member.instance.server.name, "since": moment},
            key=f"{caught_up}{member_id}:{moment}",
        )


def _lags_since(member: GroupMember, moment: str) -> bool:
    return member.lagging_since is not None and member.lagging_since.isoformat() == moment
