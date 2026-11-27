"""Speed limits of plans (DESIGN.md 6.4): the rules of each instance follow the active keys
of users with a speed limit; tc rules by peer address for WireGuard, routes by client for XRay.
A WireGuard-family instance with a total limit of its own gets a rule for every active key, so
the keys share the total equally (see shaping.py).

Each run compares the rules an instance should have with those of its latest operation and
starts a new one only when they differ, so a change is applied within one run. A failed
operation is not repeated by itself: it is retried from the journal or replaced by the next
change. An XRay instance whose statistics went away after the latest operation is limited
again: the app re-created the container or rebuilt server.json and dropped the limits with them.
A WireGuard-family instance is limited again when the poll finds its container started anew
since the latest operation: a container re-created by the app or by a change of its settings
has lost the tc rules and the script that brings them back. After a plain restart start.sh has
already put them back, and the new operation only writes the same rules again. It is limited
again, too, when the poll finds the script, the tc rules or the IFB device gone
(`TrafficSource.shaping`): after a reboot the host may have no ifb module, and start.sh goes on
without the upload limits; the new operation loads the module or polices upload.

The clone of a split group on its relay carries the clients of the leading instance; its
script has the same rules (`clone_speed_script`), and the relay deployment writes it. Another
member of a group that the relay forwards the clients to, in forward routing or the emergency
forward of a split group, carries them too: `speed_stand_ins` names it, and it gets the rules and
the total of the leading instance instead of its own. Once the clients go elsewhere it has its
own again, so the limits move with them and act in one place only.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address
from typing import Any

from django.db.models import Q

from amnezia_core.containers import WgContainer
from amnezia_core.xray import XrayContainer
from apps.core.services.providers import Providers
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.plans.models import UserLimits
from apps.plans.services.limits import PLAN_SERVERS, EffectiveLimits
from apps.protocols.models import ProtocolInstance
from apps.traffic.models import Shaping, TrafficSource
from apps.traffic.services.shaping import SpeedRule, TotalSpeed, speed_script
from apps.traffic.services.xray_shaping import XraySpeedRule

SPEED_APPLY = "traffic.apply_speed"
# The journal action of a host found without the ifb module: upload is policed from now on.
NO_IFB = "traffic.no_ifb"

_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


RulesJson = list[dict[str, Any]]


class SpeedStandIns(Providers[tuple[str, str]]):
    """Instances that carry the clients of another one now, as (their source, the source of
    the keys): the relay forwards the clients of a group to a member that is not leading.
    Usage: ``@speed_stand_ins.register`` on a function that yields the pairs.
    """


speed_stand_ins = SpeedStandIns()


class SpeedLimiter:
    def sync_all(self) -> list[Operation]:
        """Start an operation for every instance whose rules changed; return what started."""
        stand_ins = dict(speed_stand_ins)
        wanted = self._wanted(stand_ins)
        found = list(instances)
        by_source = {instance.source: instance for instance in found}
        started = []
        for instance in found:
            if not isinstance(instance.container, WgContainer | XrayContainer):
                continue  # other protocols have no speed limits
            rules = wanted.get(instance.source, [])
            params: dict[str, Any] = {"source": instance.source, "rules": rules}
            if isinstance(instance.container, WgContainer):
                params["started"] = _container_started(instance.source)
                keys = stand_ins.get(instance.source, instance.source)
                total = _total_of(by_source.get(keys, instance))
                if total:
                    params["total"] = total.to_json()
            if self._needs_apply(instance, params):
                started.append(launch(SPEED_APPLY, params, subject=instance.title))
        return started

    @classmethod
    def wanted_rules(cls) -> dict[str, RulesJson]:
        """Rules by instance source, sorted, as operation params: one per active key of a user
        whose plan limits speed, and on a WireGuard instance with a total limit one per active key.
        A WireGuard key is found by its address, an XRay key by email. A stand-in instance has
        the rules of the keys it carries.
        """
        return cls._wanted(dict(speed_stand_ins))

    @staticmethod
    def _wanted(stand_ins: Mapping[str, str]) -> dict[str, RulesJson]:
        subscriptions = (
            UserLimits.objects.filter(plan__isnull=False)
            .select_related("plan")
            .prefetch_related(*PLAN_SERVERS)
        )
        speeds = {
            limits.user_id: speed
            for limits in subscriptions
            if (speed := EffectiveLimits.of(limits).speed_limit) is not None
        }
        xray_sources = {i.source for i in instances if isinstance(i.container, XrayContainer)}
        shared = [
            i.source for i in instances if isinstance(i.container, WgContainer) and _total_of(i)
        ]
        keys = AccessKey.objects.filter(
            Q(user_id__in=list(speeds)) | Q(source__in=shared), status=KeyStatus.ACTIVE
        ).values_list("source", "public_key", "client_ip", "user_id")
        peers: dict[str, list[SpeedRule]] = defaultdict(list)
        clients: dict[str, list[XraySpeedRule]] = defaultdict(list)
        for source, public_key, client_ip, user_id in keys:
            speed = speeds.get(user_id)
            if source in xray_sources:
                if speed is not None:
                    clients[source].append(XraySpeedRule(public_key, speed))
            elif client_ip is not None:
                peers[source].append(SpeedRule(IPv4Address(client_ip), speed))
        # A peer has one address, so rules never compare by speed.
        wanted = {source: _as_json(sorted(rules)) for source, rules in peers.items()}
        wanted.update({source: _as_json(sorted(rules)) for source, rules in clients.items()})
        for source, carried in stand_ins.items():
            wanted[source] = wanted.get(carried, [])
        return wanted

    @staticmethod
    def _needs_apply(instance: Instance, params: dict[str, Any]) -> bool:
        """A fresh instance without limits needs nothing; an unfinished operation is waited for."""
        limited = bool(params["rules"] or params.get("total"))
        latest = (
            Operation.objects.filter(kind=SPEED_APPLY, params__source=instance.source)
            .order_by("-created_at", "-pk")
            .first()
        )
        if latest is None:
            return limited
        if latest.status in _UNFINISHED:
            return False
        if any(latest.params.get(name) != params.get(name) for name in ("rules", "total")):
            return True
        if not limited or latest.status != OperationStatus.SUCCEEDED:
            return False
        if isinstance(instance.container, XrayContainer):
            return _statistics_lost(instance.source, after=latest)
        restarted = latest.params.get("started") != params["started"]
        return restarted or _shaping_lost(instance.source, after=latest)


def clone_speed_script(
    interface: str, leader: ProtocolInstance, wanted: Mapping[str, RulesJson]
) -> str:
    """The limits of the keys of `leader` and its total for another interface with its clients;
    `wanted` is `SpeedLimiter.wanted_rules()`. The script itself finds out whether the host
    has the ifb module."""
    rules = [SpeedRule.from_json(rule) for rule in wanted.get(leader.source, [])]
    total = TotalSpeed(leader.download_limit, leader.upload_limit)
    return speed_script(interface, rules, total, ifb=None)


def _total_of(instance: Instance) -> TotalSpeed:
    return TotalSpeed(instance.download_limit, instance.upload_limit)


def _as_json(rules: Iterable[SpeedRule | XraySpeedRule]) -> RulesJson:
    return [rule.to_json() for rule in rules]


def _container_started(source: str) -> str:
    """The start time of the container from the latest poll; empty before the first one."""
    return (
        TrafficSource.objects.filter(source=source).values_list("started", flat=True).first() or ""
    )


def _statistics_lost(source: str, *, after: Operation) -> bool:
    """Whether the collector found no statistics since the operation finished."""
    return TrafficSource.objects.filter(
        source=source, available=False, checked_at__gt=after.finished_at
    ).exists()


def _shaping_lost(source: str, *, after: Operation) -> bool:
    """Whether a reading since the operation finished found its limits gone from the container."""
    return (
        TrafficSource.objects.filter(source=source, checked_at__gt=after.finished_at)
        .exclude(shaping__in=("", Shaping.OK))
        .exists()
    )
