from django.apps import AppConfig


class RelaysConfig(AppConfig):
    name = "apps.relays"

    def ready(self) -> None:
        from apps.relays import operations, receivers  # noqa: F401, PLC0415  registers operation kinds
        from apps.relays.services import (  # noqa: F401, PLC0415  register the probes and clones
            service_peers,
            traffic,
        )

        receivers.connect()
