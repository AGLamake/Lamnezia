from django.http import HttpRequest

from apps.core.services.site_settings import cached_settings


def site(request: HttpRequest) -> dict[str, str]:
    """The panel name for the layouts."""
    return {"site_name": cached_settings.get().site_name}
