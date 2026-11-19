"""Operation handlers of the traffic app. Each one is safe to run again."""

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.containers import WgContainer
from amnezia_core.xray import XrayContainer
from apps.keys.services.instances import instances
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.traffic.models import TrafficSource
from apps.traffic.services.shaper import SpeedShaper, XraySpeedShaper
from apps.traffic.services.shaping import SpeedRule, TotalSpeed
from apps.traffic.services.speed import SPEED_APPLY
from apps.traffic.services.xray_shaping import XraySpeedRule


@operations.register(SPEED_APPLY, _("Apply speed limits"))
def apply_speed(ctx: OperationContext) -> None:
    """Applies the rules saved at the start, so a retry does what the original meant."""
    instance = instances.get(ctx.params["source"])
    container = instance.container
    if isinstance(container, WgContainer):
        rules = [SpeedRule.from_json(rule) for rule in ctx.params["rules"]]
        total = TotalSpeed.from_json(ctx.params.get("total"))
        with instance.connect() as host:
            policed = SpeedShaper(host, ctx, container).apply(rules, total)
        TrafficSource.objects.update_or_create(
            source=instance.source,
            defaults={"upload_policed": policed},
            create_defaults={"upload_policed": policed, "checked_at": timezone.now()},
        )
    elif isinstance(container, XrayContainer):
        xray_rules = [XraySpeedRule.from_json(rule) for rule in ctx.params["rules"]]
        with instance.connect() as host:
            XraySpeedShaper(host, ctx, instance.source).apply(xray_rules)
