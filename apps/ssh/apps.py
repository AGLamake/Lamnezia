from django.apps import AppConfig


class SshConfig(AppConfig):
    name = "apps.ssh"

    def ready(self) -> None:
        # Registers the checks of the encryption key.
        from apps.ssh import checks  # noqa: F401, PLC0415
