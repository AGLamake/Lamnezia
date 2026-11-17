"""Background jobs of the panel update: the check for a new release."""

from celery import shared_task

from apps.updates.services.releases import CHECK_TASK, PanelReleases


@shared_task(name=CHECK_TASK)
def check_release() -> str:
    release = PanelReleases.from_settings().refresh()
    return str(release.version) if release else ""
