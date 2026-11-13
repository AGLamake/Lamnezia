"""Checks over the template sources that rendering tests would miss."""

import re
from pathlib import Path

from django.conf import settings

UNCLOSED_COMMENT = re.compile(r"\{#(?![^\n]*#\})")


def test_short_comments_stay_on_one_line() -> None:
    """Django renders a `{#` comment that runs to the next line as page text."""
    roots = [
        Path(settings.BASE_DIR, "templates"),
        *Path(settings.BASE_DIR, "apps").glob("*/templates"),
    ]
    broken = [
        f"{path}:{number}"
        for root in roots
        for path in root.rglob("*.html")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if UNCLOSED_COMMENT.search(line)
    ]

    assert broken == []
