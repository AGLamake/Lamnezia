from django import template

from apps.operations.models import Operation
from apps.operations.services.registry import operations
from apps.operations.services.subjects import subjects

register = template.Library()

_BADGES = {"running": "badge-muted", "succeeded": "badge-success", "failed": "badge-danger"}


@register.filter
def operation_title(kind: str) -> str:
    return operations.title(kind)


@register.filter
def status_badge(status: str) -> str:
    """CSS class of the status badge for an operation or a step."""
    return _BADGES.get(status, "badge-muted")


@register.filter
def subject_url(operation: Operation) -> str | None:
    """Page of the object the operation is about; None when there is none."""
    return subjects.url(operation)
