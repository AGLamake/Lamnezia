from datetime import UTC, datetime
from typing import Any

import pytest

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event
from apps.notifications.services.audience import StaffAudience
from apps.notifications.services.channels import Message
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def message(event: Event, **data: Any) -> Message:
    return Message("uid", event, datetime(2026, 1, 1, tzinfo=UTC), "Title", "Text", data)


def owner_of(server: Server) -> User:
    member = UserFactory(role=Role.OWNER)
    server.moderators.add(member)
    return member


def test_a_server_event_reaches_the_owner_of_that_server_only() -> None:
    server, other = ServerFactory(), ServerFactory()
    mine, theirs = owner_of(server), owner_of(other)

    audience = StaffAudience(message(Event.SERVER_DOWN, server=server.name))

    assert audience.reaches(mine)
    assert not audience.reaches(theirs)


def test_a_user_event_reaches_the_staff_of_that_user_only() -> None:
    mine, theirs = UserFactory(role=Role.MODERATOR), UserFactory(role=Role.MODERATOR)
    anna = UserFactory(managers=[mine])

    audience = StaffAudience(message(Event.LIMIT_REACHED, user=anna.username))

    assert audience.reaches(mine)
    assert not audience.reaches(theirs)


def test_a_section_at_none_turns_its_events_off() -> None:
    member = UserFactory(role=Role.MODERATOR, access={"inbox": "none"})
    anna = UserFactory(managers=[member])

    assert not StaffAudience(message(Event.CHAT_MESSAGE, user=anna.username)).reaches(member)
    assert StaffAudience(message(Event.TRAFFIC_LOW, user=anna.username)).reaches(member)


def test_a_moderator_hears_of_servers_only_when_given_the_section() -> None:
    server = ServerFactory()
    unseen = UserFactory(role=Role.MODERATOR)
    given = UserFactory(role=Role.MODERATOR, access={"servers": "view"})
    server.moderators.add(unseen, given)

    audience = StaffAudience(message(Event.SERVER_LOAD_HIGH, server=server.name))

    assert not audience.reaches(unseen)
    assert audience.reaches(given)


@pytest.mark.parametrize(
    ("event", "data"),
    [
        (Event.RELAY_DOWN, {"relay": "edge"}),
        (Event.STAFF_REPORT, {}),
        (Event.PANEL_ARCHIVE_FAILED, {"error": "disk full"}),
        (Event.KEY_CONNECTED, {"user": "", "key": "phone"}),
    ],
)
def test_an_event_outside_any_zone_stays_with_the_administrators(
    event: Event, data: dict[str, Any]
) -> None:
    server = ServerFactory()

    audience = StaffAudience(message(event, server=server.name, **data))

    assert not audience.reaches(owner_of(server))
    assert audience.reaches(UserFactory(role=Role.ADMIN))


def test_only_the_active_staff_with_a_browser_are_listed() -> None:
    admin, retired = UserFactory(role=Role.ADMIN), UserFactory(role=Role.ADMIN, is_active=False)
    for member in (admin, retired, UserFactory()):
        member.push_subscriptions.create(endpoint=f"https://push.example.com/{member.pk}")
    UserFactory(role=Role.ADMIN)

    assert StaffAudience(message(Event.STAFF_REPORT)).members() == [admin]
