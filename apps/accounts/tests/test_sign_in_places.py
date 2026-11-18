"""A sign-in from a new network or browser is told apart from a known one, without the address."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import KnownSignIn
from apps.accounts.services.sign_in_places import NewPlace, SignInPlaces, network_hash
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

FIREFOX = "Firefox · Windows"


def test_the_first_sign_in_is_not_news() -> None:
    assert SignInPlaces(UserFactory()).visit("203.0.113.5", FIREFOX) is None


def test_a_known_place_is_not_news() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("203.0.113.5", FIREFOX)

    assert places.visit("203.0.113.5", FIREFOX) is None


def test_a_new_address_in_the_same_network_is_not_news() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("203.0.113.5", FIREFOX)

    assert places.visit("203.0.113.77", FIREFOX) is None


def test_a_new_network_is_news() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("203.0.113.5", FIREFOX)

    assert places.visit("198.51.100.9", FIREFOX) == NewPlace("198.51.100.9", FIREFOX, True, False)


def test_a_new_browser_is_news() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("203.0.113.5", FIREFOX)

    assert places.visit("203.0.113.5", "Chrome · Android") == NewPlace(
        "203.0.113.5", "Chrome · Android", False, True
    )


def test_an_unknown_address_compares_only_the_browser() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("203.0.113.5", FIREFOX)

    assert places.visit(None, FIREFOX) is None


def test_ipv6_compares_the_64_network() -> None:
    places = SignInPlaces(UserFactory())
    places.visit("2001:db8:1:2::10", FIREFOX)

    assert places.visit("2001:db8:1:2::99", FIREFOX) is None
    assert places.visit("2001:db8:1:3::10", FIREFOX) is not None


def test_the_address_is_not_kept() -> None:
    user = UserFactory()
    SignInPlaces(user).visit("203.0.113.5", FIREFOX)

    row = KnownSignIn.objects.get(user=user)
    assert "203.0.113" not in row.network
    assert row.network == network_hash("203.0.113.5")


def test_a_place_not_seen_for_long_is_forgotten() -> None:
    user = UserFactory()
    places = SignInPlaces(user)
    places.visit("203.0.113.5", FIREFOX)
    places.visit("198.51.100.9", FIREFOX)
    KnownSignIn.objects.filter(network=network_hash("203.0.113.5")).update(
        last_seen=timezone.now() - SignInPlaces.FORGET_AFTER - timedelta(days=1)
    )

    assert places.visit("203.0.113.5", FIREFOX) is not None
