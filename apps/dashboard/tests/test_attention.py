"""What needs a staff member's attention: VPS payments, failed operations, relays and groups."""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.attention import Attention
from apps.groups.models import ServerGroup
from apps.operations.tests.factories import FailedOperationFactory
from apps.relays.models import Relay
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def paid_in(days: int) -> None:
    ServerFactory(name=f"vps{days}", paid_until=timezone.localdate() + timedelta(days=days))


def texts(attention: Attention) -> list[str]:
    return [notice.text for notice in attention.notices()]


def test_the_admin_is_reminded_of_vps_payments_soon_and_overdue() -> None:
    paid_in(3)
    paid_in(0)
    paid_in(-2)
    paid_in(30)

    notices = Attention(UserFactory(role=Role.ADMIN)).notices()

    assert {notice.text for notice in notices} == {
        "vps3: pay for the VPS within 3 days",
        "vps0: VPS payment is due today",
        "vps-2: VPS payment overdue by 2 days",
    }
    assert {notice.level for notice in notices if "overdue" in notice.text} == {"danger"}


def test_a_moderator_is_not_told_about_payments() -> None:
    paid_in(1)

    assert Attention(UserFactory(role=Role.MODERATOR)).payment_due() == []


def test_failed_operations_of_the_last_day_count() -> None:
    admin = UserFactory(role=Role.ADMIN)
    moderator = UserFactory(role=Role.MODERATOR)
    FailedOperationFactory(created_by=admin)
    FailedOperationFactory(created_by=moderator)

    assert Attention(admin).failed_operations == 2
    assert Attention(moderator).failed_operations == 1
    assert Attention(admin, now=timezone.now() + timedelta(days=2)).failed_operations == 0


def test_the_notice_names_how_many_operations_failed() -> None:
    admin = UserFactory(role=Role.ADMIN)
    FailedOperationFactory(created_by=admin)

    assert "1 operation failed in the last day" in texts(Attention(admin))


def test_without_trouble_there_are_no_notices() -> None:
    ServerFactory()

    assert Attention(UserFactory(role=Role.ADMIN)).notices() == []


def relayed_group(alive: list[str], down: list[str], read_error: str = "") -> ServerGroup:
    relay = ServerFactory(name="relay")
    group = ServerGroup.objects.create(name="Europe", port=51820, relay=relay)
    Relay.objects.create(
        server=relay,
        installed_at=timezone.now(),
        health=reading(group, alive, down),
        read_error=read_error,
    )
    return group


def reading(group: ServerGroup, alive: list[str], down: list[str]) -> dict[str, Any]:
    members = {name: {"id": number, "name": name} for number, name in enumerate(alive + down)}
    state = {
        "port": group.port,
        "active": None,
        "alive": [members[name] for name in alive],
        "down": [members[name] for name in down],
    }
    return {"updated_at": 1_700_000_000, "groups": [state]}


def test_a_group_without_a_live_member_is_told_until_one_answers() -> None:
    admin = UserFactory(role=Role.ADMIN)
    group = relayed_group(alive=[], down=["de1", "de2"])

    notices = Attention(admin).notices()

    assert [notice.url for notice in notices] == [reverse("groups:detail", args=[group.pk])]
    assert notices[0].text == "Group Europe: no member answers the relay"
    Relay.objects.update(health=reading(group, alive=["de1"], down=["de2"]))
    assert Attention(admin).notices() == []


def test_a_relay_whose_state_cannot_be_read_is_told_instead_of_its_old_reading() -> None:
    relayed_group(alive=[], down=["de1"], read_error="stale")
    relay = Relay.objects.get()

    notices = Attention(UserFactory(role=Role.ADMIN)).notices()

    assert [notice.url for notice in notices] == [reverse("servers:detail", args=[relay.server_id])]
    assert notices[0].text == "Relay relay: its state cannot be read"


def test_a_moderator_is_not_told_about_relays() -> None:
    relayed_group(alive=[], down=["de1"], read_error="stale")

    assert Attention(UserFactory(role=Role.MODERATOR)).notices() == []
