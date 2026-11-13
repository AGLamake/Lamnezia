from django.apps import AppConfig


class KeysConfig(AppConfig):
    name = "apps.keys"

    def ready(self) -> None:
        # Registers the key operations with the operation journal.
        from apps.keys import operations  # noqa: F401, PLC0415
