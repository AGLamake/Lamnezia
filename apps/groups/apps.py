from django.apps import AppConfig


class GroupsConfig(AppConfig):
    name = "apps.groups"

    def ready(self) -> None:
        from apps.groups import operations  # noqa: F401, PLC0415  registers operation kinds
