"""Helpers for the registration tests."""

from typing import Any

import factory

from apps.accounts.models import Invite
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings


class InviteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Invite

    plan = factory.SubFactory("apps.plans.tests.factories.PlanFactory")


def configure(**fields: Any) -> SiteSettings:
    """Save panel settings the way the settings page does and drop the cached copy."""
    site = SiteSettings.load()
    for name, value in fields.items():
        setattr(site, name, value)
    site.save()
    cached_settings.invalidate()
    return site
