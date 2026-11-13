from django.apps import AppConfig


class ServersConfig(AppConfig):
    name = "apps.servers"

    def ready(self) -> None:
        from apps.servers import operations  # noqa: F401, PLC0415  registers operation kinds
