"""The owner hears once that a new key has connected (`key_connected`)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification
from apps.notifications.services.first_connection import FirstConnections
from apps.traffic.models import TrafficCounter

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

SOURCE = "instance:1"


def issued(name: str = "iPhone", **fields: Any) -> AccessKey:
    fields.setdefault("user", UserFactory())
    fields.setdefault("status", KeyStatus.ACTIVE)
    return AccessKey.objects.create(
        source=SOURCE, name=name, public_key=f"key-{AccessKey.objects.count()}", **fields
    )


def counter(key: AccessKey, source: str = SOURCE, **seen: datetime | None) -> TrafficCounter:
    return TrafficCounter.objects.create(
        source=source,
        key_source=key.source,
        public_key=key.public_key,
        collected_at=timezone.now(),
        **seen,
    )


def tell() -> list[dict[str, Any]]:
    """Run the check; return the details of every `key_connected` so far."""
    FirstConnections().tell_all(timezone.now())
    return [n.data for n in Notification.objects.filter(event=Event.KEY_CONNECTED).order_by("pk")]


def test_the_first_handshake_after_the_issue_is_told_once() -> None:
    key = issued()
    counter(key, latest_handshake=timezone.now())

    tell()
    [data] = tell()

    assert key.user is not None
    assert (data["user"], data["key"], data["key_id"]) == (key.user.username, "iPhone", key.pk)
    assert data["url"].endswith(f"/keys/{key.pk}/")
    key.refresh_from_db()
    assert key.connected_at is not None


def test_counters_that_grew_count_without_a_handshake() -> None:
    key = issued()
    counter(key, active_at=timezone.now())

    assert len(tell()) == 1


def test_a_key_not_yet_in_use_is_not_told() -> None:
    key = issued()
    counter(key)
    counter(issued(), latest_handshake=datetime(2020, 1, 1, tzinfo=UTC))

    assert tell() == []
    key.refresh_from_db()
    assert key.connected_at is None


def test_a_key_of_a_group_connects_on_any_member() -> None:
    key = issued()
    counter(key)
    counter(key, source="instance:2", latest_handshake=timezone.now())

    assert len(tell()) == 1


def test_a_reissued_key_of_the_same_device_is_not_told_again() -> None:
    old = issued(connected_at=timezone.now() - timedelta(days=3), status=KeyStatus.REVOKED)
    new = issued(user=old.user)
    counter(new, latest_handshake=timezone.now())
    other = issued(name="Laptop", user=old.user)
    counter(other, latest_handshake=timezone.now())

    [data] = tell()

    assert data["key"] == "Laptop"
    new.refresh_from_db()
    assert new.connected_at is not None


def test_imported_and_disabled_keys_are_not_told() -> None:
    counter(issued(imported=True), latest_handshake=timezone.now())
    counter(issued(status=KeyStatus.DISABLED_MANUAL), latest_handshake=timezone.now())

    assert tell() == []


def test_the_event_goes_to_the_owner_and_reads_well() -> None:
    kind = EVENTS[Event.KEY_CONNECTED]
    title, text = kind.render({"user": "ann", "key": "iPhone", "server": "Amsterdam"})

    assert kind.to_user
    assert title == "Your key iPhone works"
    assert "Amsterdam" in text
