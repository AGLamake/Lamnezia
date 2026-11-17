from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class UpdatesConfig(AppConfig):
    name = "apps.updates"
    verbose_name = _("Panel update")

    def ready(self) -> None:
        # Registers the update operation with the operation journal.
        from apps.updates import operations  # noqa: F401, PLC0415
