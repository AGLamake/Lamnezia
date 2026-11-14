from django.apps import AppConfig


class RelaysConfig(AppConfig):
    name = "apps.relays"

    def ready(self) -> None:
        from apps.relays import operations  # noqa: F401, PLC0415  registers operation kinds
        from apps.relays.services import service_peers  # noqa: F401, PLC0415  registers the probes
