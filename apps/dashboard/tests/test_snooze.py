"""Hiding a notice of the overview until tomorrow."""

from datetime import date, timedelta

import pytest
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.attention import Notice
from apps.dashboard.services.snooze import Snooze
from apps.guides.tests.test_materials import material

TODAY = date(2026, 11, 12)
WARNING = "useful material"


def notice(key: str) -> Notice:
    return Notice("info", "inbox", key, "/", key)


def test_a_hidden_notice_is_left_out_today_only() -> None:
    session = SessionStore()
    Snooze(session, TODAY).hide("offline:3")
    notices = [notice("offline:3"), notice("unread")]

    assert Snooze(session, TODAY).split(notices) == ([notices[1]], 1)
    assert Snooze(session, TODAY + timedelta(days=1)).split(notices) == (notices, 0)


def test_hiding_drops_the_keys_of_earlier_days() -> None:
    session = SessionStore()
    Snooze(session, TODAY - timedelta(days=1)).hide("unread")
    Snooze(session, TODAY).hide("failed-operations")

    assert Snooze(session, TODAY).hidden() == {"failed-operations"}
    assert list(session["hidden_notices"]) == ["failed-operations"]


@pytest.mark.parametrize("key", ["offline:3", "broken-links", "payment:12"])
def test_keys_of_the_notices_are_valid(key: str) -> None:
    assert Snooze.valid(key)


@pytest.mark.parametrize("key", ["", "x" * 40, "offline:3; drop", "Unread"])
def test_other_keys_are_not(key: str) -> None:
    assert not Snooze.valid(key)


@pytest.mark.django_db
def test_the_overview_hides_a_notice_and_shows_it_again(client: Client) -> None:
    material("dead", is_broken=True)
    client.force_login(UserFactory(role=Role.ADMIN))
    index = reverse("dashboard:index")

    client.post(reverse("dashboard:notice-hide"), {"key": "broken-links"})
    hidden = client.get(index).content.decode()
    client.post(reverse("dashboard:notices-show"))
    shown = client.get(index).content.decode()

    assert WARNING not in hidden
    assert "Show 1 hidden until tomorrow" in hidden
    assert WARNING in shown


@pytest.mark.django_db
def test_a_made_up_key_is_refused(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    response = client.post(reverse("dashboard:notice-hide"), {"key": "<script>"})

    assert response.status_code == 400
