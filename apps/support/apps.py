from django.apps import AppConfig


class SupportConfig(AppConfig):
    name = "apps.support"

    def ready(self) -> None:
        from apps.support import operations  # noqa: F401, PLC0415  registers operation kinds
