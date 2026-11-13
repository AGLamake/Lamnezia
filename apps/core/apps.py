from typing import Any

from django.apps import AppConfig
from django.db import transaction
from django.db.models.signals import post_save


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self) -> None:
        from apps.core.models import SiteSettings  # noqa: PLC0415
        from apps.core.services.site_settings import cached_settings  # noqa: PLC0415

        def settings_saved(**kwargs: Any) -> None:
            # Other processes see the change once it is committed, not before.
            transaction.on_commit(cached_settings.invalidate)

        post_save.connect(settings_saved, sender=SiteSettings, weak=False)
