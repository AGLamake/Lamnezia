from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"

    def ready(self) -> None:
        from apps.notifications import receivers  # noqa: PLC0415

        receivers.connect()
