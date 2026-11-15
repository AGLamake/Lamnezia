from django.apps import AppConfig


class GroupsConfig(AppConfig):
    name = "apps.groups"

    def ready(self) -> None:
        # Registers the operation kinds and the key groups.
        from apps.groups import operations, receivers  # noqa: F401, PLC0415
        from apps.groups.services import replication  # noqa: F401, PLC0415

        receivers.connect()
