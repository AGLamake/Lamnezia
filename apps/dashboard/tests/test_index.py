"""The dashboard start page: the warning about broken links of useful materials."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.guides.tests.test_materials import material

pytestmark = pytest.mark.django_db

WARNING = "useful material"


def dashboard(client: Client, role: Role) -> str:
    client.force_login(UserFactory(role=role))
    return client.get(reverse("dashboard:index")).content.decode()


def test_the_admin_sees_how_many_published_links_are_broken(client: Client) -> None:
    material("dead-one", is_broken=True)
    material("dead-two", is_broken=True)
    material("dead-hidden", is_broken=True, is_published=False)
    material("alive")

    page = dashboard(client, Role.ADMIN)

    assert "2 useful materials are hidden" in page
    assert reverse("admin:guides_usefulmaterial_changelist") + "?is_broken__exact=1" in page


def test_without_broken_links_there_is_no_warning(client: Client) -> None:
    material("alive")

    assert WARNING not in dashboard(client, Role.ADMIN)


def test_a_moderator_who_cannot_fix_them_is_not_told(client: Client) -> None:
    material("dead", is_broken=True)

    assert WARNING not in dashboard(client, Role.MODERATOR)
