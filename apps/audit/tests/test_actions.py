"""Readable names of journal actions, on the page and in the filter."""

import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.services import journal as audit
from apps.audit.services.actions import action_label

pytestmark = pytest.mark.django_db


def test_an_action_of_an_operation_reads_as_its_title() -> None:
    assert action_label("keys.issue") == "Issue key"


def test_an_action_without_an_operation_is_named_too() -> None:
    assert action_label("plans.create") == "Create plan"


def test_an_unknown_action_reads_as_its_code() -> None:
    assert action_label("system.start") == "system.start"


def test_the_page_shows_names_and_keeps_codes_in_hints(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    audit.log("plans.create", actor=admin)
    client.force_login(admin)

    html = client.get("/audit/").content.decode()

    assert '<option value="plans.create">Create plan</option>' in html
    assert 'title="plans.create"><span class="font-medium">Create plan</span>' in html
