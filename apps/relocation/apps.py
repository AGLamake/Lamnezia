from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RelocationConfig(AppConfig):
    name = "apps.relocation"
    verbose_name = _("Panel move")

    def ready(self) -> None:
        # Registers the move operations with the operation journal.
        from apps.relocation import operations  # noqa: F401, PLC0415
