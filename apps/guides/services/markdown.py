"""Markdown of FAQ articles to safe HTML."""

import markdown
import nh3
from django.utils.safestring import SafeString, mark_safe

_EXTENSIONS = ["extra", "sane_lists"]


def render_markdown(text: str) -> SafeString:
    """Raw HTML in the source is cleaned out: moderators edit articles, not only the admin."""
    html = markdown.markdown(text, extensions=_EXTENSIONS)
    return mark_safe(nh3.clean(html))
