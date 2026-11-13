from django.apps import AppConfig


class BackupsConfig(AppConfig):
    name = "apps.backups"

    def ready(self) -> None:
        # Registers the restore operation with the operation journal.
        from apps.backups import operations, receivers  # noqa: F401, PLC0415

        receivers.connect()
