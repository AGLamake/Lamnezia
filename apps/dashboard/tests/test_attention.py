"""What needs a staff member's attention: VPS payments, failed operations, relays and groups."""

from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.attention import Attention
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import JOIN_KIND, LEAVE_KIND
from apps.operations.models import Operation, OperationStatus
from apps.operations.tests.factories import FailedOperationFactory
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay
from apps.servers.models import InterfaceRepair, LoadAlert, LoadResource, Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficSource

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


def test_the_payment_reminder_links_to_the_hosting_page(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    ServerFactory(
        paid_until=timezone.localdate() + timedelta(days=2),
        payment_url="https://billing.example.com/vps/7",
    )
    client.force_login(admin)

    [notice] = Attention(admin).notices()
    html = client.get(reverse("dashboard:index")).content.decode()

    assert notice.external == "https://billing.example.com/vps/7"
    assert 'href="https://billing.example.com/vps/7"' in html


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


def test_a_relay_that_keeps_other_settings_after_a_write_is_told() -> None:
    relayed_group(alive=["de1"], down=[])
    written = timezone.now() - timedelta(minutes=5)
    mismatch = {"foreign_ports": [5542], "missing_ports": [], "groups": []}
    Relay.objects.update(mismatch=mismatch, synced="digest", synced_at=written, read_at=written)
    admin = UserFactory(role=Role.ADMIN)
    assert Attention(admin).notices() == [], "not read since the write"

    Relay.objects.update(read_at=timezone.now())
    relay = Relay.objects.get()
    notices = Attention(admin).notices()

    assert [notice.url for notice in notices] == [reverse("servers:detail", args=[relay.server_id])]
    assert notices[0].text == "Relay relay: other settings stay after the panel wrote it again"
    Relay.objects.update(synced="")
    assert Attention(admin).notices() == [], "a write is due"


def test_a_moderator_is_not_told_about_relays() -> None:
    relayed_group(alive=[], down=["de1"], read_error="stale")

    assert Attention(UserFactory(role=Role.MODERATOR)).notices() == []


def member_of(group: ServerGroup, **fields: Any) -> GroupMember:
    instance = ProtocolInstance.objects.create(
        server=ServerFactory(), container="amnezia-awg2", port=group.port
    )
    return GroupMember.objects.create(group=group, instance=instance, priority=1, **fields)


def test_groups_with_trouble_are_counted_once_each() -> None:
    admin = UserFactory(role=Role.ADMIN)
    down = relayed_group(alive=[], down=["de1"])
    member_of(down, lagging_since=timezone.now())
    lagging = ServerGroup.objects.create(name="Asia", port=51821)
    member_of(lagging, lagging_since=timezone.now())
    stuck = ServerGroup.objects.create(name="America", port=51822)
    member = member_of(stuck, state=MemberState.LEAVING)
    Operation.objects.create(
        kind=LEAVE_KIND, params={"member_id": member.pk}, status=OperationStatus.FAILED
    )
    leaving = ServerGroup.objects.create(name="Africa", port=51823)
    under_way = member_of(leaving, state=MemberState.LEAVING)
    Operation.objects.create(kind=LEAVE_KIND, params={"member_id": under_way.pk})
    joining = ServerGroup.objects.create(name="Oceania", port=51824)
    member = member_of(joining, state=MemberState.JOINING)
    Operation.objects.create(
        kind=JOIN_KIND, params={"member_id": member.pk}, status=OperationStatus.FAILED
    )

    assert Attention(admin).group_problems == 4
    assert Attention(UserFactory(role=Role.MODERATOR)).group_problems == 0


def test_the_menu_counts_groups_with_trouble(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    client.force_login(admin)
    group = ServerGroup.objects.create(name="Asia", port=51821)
    member_of(group, lagging_since=timezone.now())

    page = client.get(reverse("dashboard:index")).content.decode()

    assert reverse("groups:list") in page
    assert "Groups with a lagging or stuck server" in page


def overloaded(name: str, *resources: LoadResource) -> Server:
    server = ServerFactory(name=name)
    server.record_sample(ServerSample.objects.create(server=server, at=timezone.now()))
    for resource in resources:
        LoadAlert.objects.create(server=server, resource=resource, high_since=timezone.now())
    return server


def test_a_server_under_high_load_needs_attention_and_shows_it_on_its_card(
    client: Client,
) -> None:
    admin = UserFactory(role=Role.ADMIN)
    server = overloaded("Germany", LoadResource.DISK, LoadResource.CPU)
    calm = overloaded("Finland")
    LoadAlert.objects.create(server=calm, resource=LoadResource.CPU, told_at=timezone.now())
    client.force_login(admin)

    [notice] = [n for n in Attention(admin).notices() if n.key.startswith("overload:")]
    card = client.get(reverse("servers:detail", args=[server.pk])).content.decode()

    assert (notice.text, notice.level) == ("Germany: high load — CPU, Disk", "warning")
    assert "High load: CPU above the threshold since" in card
    assert "High load: Disk above the threshold since" in card


def test_a_server_that_does_not_answer_is_not_told_as_overloaded() -> None:
    server = overloaded("Germany", LoadResource.CPU)
    Server.objects.filter(pk=server.pk).update(last_seen_at=timezone.now() - timedelta(days=1))

    assert Attention(UserFactory(role=Role.ADMIN)).overloaded() == []


def test_a_server_without_the_ifb_module_is_told() -> None:
    policed = ProtocolInstance.objects.create(
        server=ServerFactory(name="Germany"), container="amnezia-awg2", port=51820
    )
    shaped = ProtocolInstance.objects.create(
        server=ServerFactory(name="Finland"), container="amnezia-awg2", port=51820
    )
    for instance, flag in ((policed, True), (shaped, False)):
        TrafficSource.objects.create(
            source=instance.source, checked_at=timezone.now(), upload_policed=flag
        )

    [notice] = [n for n in Attention(UserFactory(role=Role.ADMIN)).notices() if "ifb" in n.key]

    assert notice.text == "Germany: no ifb module, upload over the speed limits is dropped"
    assert notice.key == f"no-ifb:{policed.server_id}"


def test_a_container_the_restarts_did_not_bring_back_needs_attention() -> None:
    server = ServerFactory(name="Germany")
    since = timezone.now()
    InterfaceRepair.objects.create(
        server=server, container="amnezia-awg2", missing_since=since, attempts=3, gave_up=True
    )
    InterfaceRepair.objects.create(
        server=server, container="amnezia-awg", missing_since=since, attempts=1
    )

    [notice] = [
        n
        for n in Attention(UserFactory(role=Role.ADMIN)).notices()
        if n.key.startswith("interface-repair:")
    ]

    assert notice.text == "Germany: amnezia-awg2 has no VPN interface, restarts did not help"
    assert (notice.level, notice.url) == ("danger", reverse("servers:detail", args=[server.pk]))
