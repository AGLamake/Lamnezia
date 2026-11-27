from django.apps import AppConfig


class BotsConfig(AppConfig):
    name = "apps.bots"

    def ready(self) -> None:
        # Keeps the Telegram menus in step with roles and links.
        from apps.bots import receivers  # noqa: PLC0415

        receivers.connect()
