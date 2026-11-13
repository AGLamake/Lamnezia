"""Template tag that tells whether users can pay in the panel."""

from django import template

from apps.billing.services.providers import payment_providers

register = template.Library()


@register.simple_tag
def billing_enabled() -> bool:
    """True when a payment plugin is installed and available."""
    return bool(payment_providers.available())
