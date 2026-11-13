"""Template filter `action_label`: the readable name of an audit journal action."""

from django import template

from apps.audit.services.actions import action_label as _action_label

register = template.Library()


@register.filter
def action_label(action: str) -> str:
    return _action_label(action)
