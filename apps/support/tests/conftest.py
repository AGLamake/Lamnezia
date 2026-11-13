from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    """Screenshots of the tests land in a temporary folder."""
    settings.MEDIA_ROOT = tmp_path
