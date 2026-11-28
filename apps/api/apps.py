from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "apps.api"

    def ready(self) -> None:
        # A retry of a token's operation stays visible to the token.
        from apps.api import receivers  # noqa: PLC0415

        receivers.connect()
