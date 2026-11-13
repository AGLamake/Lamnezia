"""Background jobs of the guides: the Amnezia app release and the check of useful material links."""

from celery import shared_task

from apps.guides.services.materials import MaterialLinkCheck
from apps.guides.services.releases import LatestReleaseProvider


@shared_task(name="guides.refresh_release")
def refresh_release() -> None:
    LatestReleaseProvider.from_settings().refresh()


@shared_task(name="guides.check_materials")
def check_materials() -> int:
    return MaterialLinkCheck.from_settings().run()
