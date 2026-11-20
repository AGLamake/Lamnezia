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
already put them back, and the new operation only writes the same rules again.

The clone of a split group on its relay carries the clients of the leading instance; its
script has the same rules (`clone_speed_script`), and the relay deployment writes it.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address
from typing import Any

from django.db.models import Q

from amnezia_core.containers import WgContainer
from amnezia_core.xray import XrayContainer
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.plans.models import UserLimits
from apps.plans.services.limits import PLAN_SERVERS, EffectiveLimits
from apps.protocols.models import ProtocolInstance
from apps.traffic.models import TrafficSource
from apps.traffic.services.shaping import SpeedRule, TotalSpeed, speed_script
from apps.traffic.services.xray_shaping import XraySpeedRule

SPEED_APPLY = "traffic.apply_speed"

_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


RulesJson = list[dict[str, Any]]


class SpeedLimiter:
    def sync_all(self) -> list[Operation]:
        """Start an operation for every instance whose rules changed; return what started."""
        wanted = self.wanted_rules()
        started = []
        for instance in instances:
            if not isinstance(instance.container, WgContainer | XrayContainer):
                continue  # other protocols have no speed limits
            rules = wanted.get(instance.source, [])
            params: dict[str, Any] = {"source": instance.source, "rules": rules}
            if isinstance(instance.container, WgContainer):
                params["started"] = _container_started(instance.source)
                total = _total_of(instance)
                if total:
                    params["total"] = total.to_json()
            if self._needs_apply(instance, params):
                started.append(launch(SPEED_APPLY, params, subject=instance.title))
        return started

    @staticmethod
    def wanted_rules() -> dict[str, RulesJson]:
        """Rules by instance source, sorted, as operation params: one per active key of a user
        whose plan limits speed, and on a WireGuard instance with a total limit one per active key.
        A WireGuard key is found by its address, an XRay key by email.
        """
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
        return bool(latest.params.get("started") != params["started"])


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
