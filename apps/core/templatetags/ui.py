"""Template tags of the design system: `icon` (inline SVG), `sparkline` and `bar_chart` (charts)."""

from collections.abc import Iterable
from typing import Any

from django import forms, template
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from apps.core.services.bar_chart import StackedBars
from apps.core.services.sparkline import Sparkline

register = template.Library()

# Labels under a bar chart on a phone: more would run into each other.
_NARROW_AXIS_LABELS = 3

# Stroke icons on a 24 x 24 grid, drawn for the panel (no third-party set to credit).
ICONS: dict[str, str] = {
    "menu": "M4 6h16M4 12h16M4 18h16",
    "close": "M6 6l12 12M18 6L6 18",
    "sun": (
        "M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4"
        "M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
    ),
    "moon": "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
    "monitor": "M3 5h18v11H3zM8 20h8M12 16v4",
    "globe": (
        "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM3 12h18M12 3c3 3.5 3 14.5 0 18"
        "M12 3c-3 3.5-3 14.5 0 18"
    ),
    "user": "M12 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8zM4 20c1.5-4 4.5-6 8-6s6.5 2 8 6",
    "shield": "M12 3l8 3v6c0 4.5-3.5 8-8 9c-4.5-1-8-4.5-8-9V6zM9 12l2 2l4-4",
    "logout": "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 16l-4-4l4-4M6 12h10",
    "chevron-down": "M6 9l6 6l6-6",
    "chevron-right": "M9 6l6 6l-6 6",
    "chevron-left": "M15 6l-6 6l6 6",
    "search": "M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14zM20 20l-4-4",
    "check": "M5 12l5 5L20 7",
    "info": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 11v5M12 8h.01",
    "warning": "M12 3L2 20h20zM12 10v4M12 17h.01",
    "error": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM9 9l6 6M15 9l-6 6",
    "success": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM8 12l3 3l5-6",
    "inbox": "M3 13l3-8h12l3 8v6H3zM3 13h5l1 3h6l1-3h5",
    "home": "M4 11l8-7l8 7M6 9.5V20h12V9.5",
    "server": "M4 4h16v7H4zM4 13h16v7H4zM8 7.5h.01M8 16.5h.01",
    "key": "M8 11a4 4 0 1 0 0 8a4 4 0 1 0 0-8zM10.8 12.2L20 3M16 7l3 3M14 9l2 2",
    "chart": "M4 20V4M4 20h16M8 16v-5M12 16V8M16 16v-3",
    "users": (
        "M9 4a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7zM2 20c1-3.5 3.8-5.5 7-5.5s6 2 7 5.5"
        "M16 4.5a3.5 3.5 0 0 1 0 6.5M18.5 14.8c1.7.8 2.9 2.6 3.5 5.2"
    ),
    "settings": (
        "M18.7 9.9L21.4 10.4V13.6L18.7 14.1L18.2 15.3L19.8 17.5L17.5 19.8L15.3 18.2L14.1 18.7"
        "L13.6 21.4H10.4L9.9 18.7L8.7 18.2L6.5 19.8L4.2 17.5L5.8 15.3L5.3 14.1L2.6 13.6V10.4"
        "L5.3 9.9L5.8 8.7L4.2 6.5L6.5 4.2L8.7 5.8L9.9 5.3L10.4 2.6H13.6L14.1 5.3L15.3 5.8"
        "L17.5 4.2L19.8 6.5L18.2 8.7zM12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z"
    ),
    "plus": "M12 5v14M5 12h14",
    "trash": "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    "copy": "M9 9h11v11H9zM5 15H4V4h11v1",
    "external": "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
    "share": (
        "M18 3a3 3 0 1 0 0 6a3 3 0 1 0 0-6zM6 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z"
        "M18 15a3 3 0 1 0 0 6a3 3 0 1 0 0-6zM8.6 13.5l6.8 4M15.4 6.5l-6.8 4"
    ),
    "download": "M12 4v11M7 10l5 5l5-5M5 20h14",
    "phone": "M7 2h10v20H7zM11 18h2",
    "book": "M4 5a2 2 0 0 1 2-2h14v15H6a2 2 0 0 0-2 2zM4 20a2 2 0 0 0 2 2h14v-4",
    "wallet": "M3 7h18v13H3zM3 7l13-4v4M16 13h2",
    "refresh": "M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4",
    "pencil": "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
    "dashboard": "M4 4h7v9H4zM13 4h7v5h-7zM13 11h7v9h-7zM4 15h7v5H4z",
    "activity": "M3 12h4l3-8l4 16l3-8h4",
    "tag": "M12.6 3H4a1 1 0 0 0-1 1v8.6l9.4 9.4l9.6-9.6zM7.5 7.5h.01",
    "user-plus": (
        "M10 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8zM3 20c1.3-3.8 3.9-6 7-6c1.6 0 3.1.6 4.3 1.6"
        "M19 14v6M16 17h6"
    ),
    "bell": "M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 21h4",
    "receipt": "M5 3h14v18l-3.5-2l-3.5 2l-3.5-2L5 21zM9 8h6M9 12h6M9 16h3",
    "file-text": "M14 3H6v18h12V7zM14 3v4h4M9 13h6M9 17h6M9 9h2",
    "history": "M3 12a9 9 0 1 0 2.6-6.4L3 8M3 3v5h5M12 7v5l3 2",
    "cpu": "M7 7h10v10H7zM10 10h4v4h-4zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4",
    "disk": "M3 14h18v6H3zM5.5 14L8 5h8l2.5 9M7 17h.01",
    "arrow-down": "M12 5v14M6 13l6 6l6-6",
    "arrow-up": "M12 19V5M6 11l6-6l6 6",
    "signal": "M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01",
    "calendar": "M4 5h16v16H4zM4 10h16M8 3v4M16 3v4",
    "filter": "M3 5h18l-7 8v6l-4 2v-8z",
    "zap": "M13 2L4 14h7l-1 8l9-12h-7z",
    "database": (
        "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3s-3.6 3-8 3s-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"
        "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"
    ),
    "radar": (
        "M12 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4zM7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4"
        "M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2"
    ),
    "layers": "M12 3l9 5l-9 5l-9-5zM3 13l9 5l9-5",
    "message": "M4 5h16v11H9l-5 4z",
    "send": "M4 12L20 4l-4 16l-4-7zM12 13l8-9",
    "paperclip": "M20 11.5l-7.8 7.8a5 5 0 0 1-7.1-7.1l8.1-8.1a3.3 3.3 0 0 1 4.7 4.7l-8.1 8.1"
    "a1.7 1.7 0 0 1-2.4-2.4l7.4-7.4",
    "template": "M4 4h16v6H4zM4 14h7v6H4zM15 14h5M15 18h5",
    "clock": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 7v5l3 2",
    "switch": "M7 4L3 8l4 4M3 8h14M17 12l4 4l-4 4M21 16H7",
    "more": "M5 12h.01M12 12h.01M19 12h.01",
    "width-normal": "M6 4h12v16H6zM10 12h4",
    "width-wide": "M3 5h18v14H3zM7 12h10M9 10l-2 2l2 2M15 10l2 2l-2 2",
    "eye": "M2 12s3.5-7 10-7s10 7 10 7s-3.5 7-10 7s-10-7-10-7zM12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z",
    "eye-off": "M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.2 3.2"
    "M6.6 6.6C3.7 8.3 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"
    "M3 3l18 18",
    # Operating systems: plain shapes, not the vendors' logos.
    "os-windows": "M4 5.5l7-1V11H4zM13 4.2l7-1.2V11h-7zM4 13h7v6.5l-7-1zM13 13h7v8l-7-1.2z",
    "os-macos": "M5 5h14v10H5zM2 19h20M10 17h4",
    "os-linux": "M3 5h18v14H3zM7 10l3 2l-3 2M12 15h5",
    "os-android": "M6 11a6 6 0 0 1 12 0zM8 6.5L6.5 4M16 6.5L17.5 4M6 13h12v6a1 1 0 0 1-1 1H7"
    "a1 1 0 0 1-1-1zM10 8.5h.01M14 8.5h.01",
    "os-ios": "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM10 4.5h4"
    "M11 19h2",
}

# Filled shapes of the messengers' marks, drawn for the panel and simplified (no files from
# their sites): Telegram's paper plane and Discord's face, whose eyes are holes (evenodd).
BRAND_ICONS: dict[str, str] = {
    "telegram": "M2.6 11.2l17-6.6c.8-.3 1.5.2 1.2 1.4l-2.9 13.6c-.2 1-.8 1.2-1.6.7l-4.4-3.2l-2.1 2"
    "c-.2.2-.4.4-.9.4l.3-4.5l8.2-7.4c.4-.3-.1-.5-.6-.2l-10.1 6.4l-4.4-1.4c-1-.3-1-1 .3-1.5z",
    "discord": "M19.3 5.3A16.6 16.6 0 0 0 15.2 4l-.5 1a15.4 15.4 0 0 0-5.4 0l-.5-1a16.6 16.6 0 0 0"
    "-4.1 1.3C2.1 9.2 1.4 13 1.7 16.7a16.8 16.8 0 0 0 5.1 2.6l1.1-1.8c-.6-.2-1.2-.5-1.7-.9l.4-.3"
    "a11.9 11.9 0 0 0 10.8 0l.4.3c-.5.4-1.1.7-1.7.9l1.1 1.8a16.8 16.8 0 0 0 5.1-2.6"
    "c.4-4.3-.6-8-2.9-11.4zM8.7 14.4c-1 0-1.8-.9-1.8-2s.8-2 1.8-2s1.8.9 1.8 2s-.8 2-1.8 2z"
    "M15.3 14.4c-1 0-1.8-.9-1.8-2s.8-2 1.8-2s1.8.9 1.8 2s-.8 2-1.8 2z",
}

_STROKE = (
    'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
    'stroke-linejoin="round"'
)
_FILL = 'fill="currentColor" fill-rule="evenodd"'


@register.simple_tag
def icon(name: str, css: str = "icon", label: str = "") -> SafeString:
    """An inline SVG icon; decorative unless `label` names it for screen readers.

    An unknown name renders nothing, so a message level without an icon (debug) still shows.
    """
    if name in ICONS:
        path, paint = ICONS[name], _STROKE
    elif name in BRAND_ICONS:
        path, paint = BRAND_ICONS[name], _FILL
    else:
        return mark_safe("")
    a11y = format_html('role="img" aria-label="{}"', label) if label else 'aria-hidden="true"'
    return format_html(
        '<svg class="{}" viewBox="0 0 24 24" {} focusable="false" {}><path d="{}"/></svg>',
        css,
        mark_safe(paint),  # a constant above
        mark_safe(a11y),  # built by format_html above
        path,
    )


@register.filter
def active_filters(form: forms.Form) -> int:
    """How many visible fields of a list filter form the request filled in."""
    if not form.is_bound:
        return 0
    return sum(1 for field in form.visible_fields() if form.data.get(field.html_name))


@register.inclusion_tag("components/sparkline.html")
def sparkline(
    values: Iterable[float | None], label: str, floor: Any = None, ceiling: Any = None
) -> dict[str, Any]:
    """A tiny line chart of `values`; `label` describes it to screen readers."""
    chart = Sparkline(
        values=list(values),
        floor=None if floor is None else float(floor),
        ceiling=None if ceiling is None else float(ceiling),
    )
    return {"chart": chart, "label": label, "segments": chart.segments(), "dot": chart.last_point()}


@register.inclusion_tag("components/bar_chart.html")
def bar_chart(
    labels: Iterable[str], series: Iterable[Iterable[int]], names: Iterable[str], label: str
) -> dict[str, Any]:
    """Stacked bars of byte counts, one series per name and one bar per label.

    `label` describes the chart to screen readers, which also get the numbers as a table.
    """
    titles = list(names)
    chart = StackedBars(labels=list(labels), series=[list(values) for values in series])
    return {
        "label": label,
        "legend": [(index + 1, title) for index, title in enumerate(titles)],
        "titles": titles,
        "several": len(titles) > 1,
        "columns": [
            (column, list(zip(titles, column.values, strict=True))) for column in chart.columns()
        ],
        "ticks": chart.ticks(),
        "axis": chart.axis_labels(),
        "axis_narrow": chart.axis_labels(_NARROW_AXIS_LABELS),
        "empty": chart.empty,
    }
