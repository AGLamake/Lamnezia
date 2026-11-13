from django.apps import AppConfig


class TrafficConfig(AppConfig):
    name = "apps.traffic"

    def ready(self) -> None:
        # Registers the traffic operations with the operation journal and the signal receivers.
        from apps.traffic import operations, receivers  # noqa: F401, PLC0415

        receivers.connect()
