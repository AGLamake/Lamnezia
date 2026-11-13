from collections.abc import Iterator

import pytest

from apps.core.services.site_settings import cached_settings


@pytest.fixture(autouse=True)
def _fresh_site_settings() -> Iterator[None]:
    """Each test rolls the database back, so no test may see SiteSettings cached by another."""
    yield
    cached_settings.invalidate()
