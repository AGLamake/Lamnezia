import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.groups.models import GroupMember, LagReason, MemberState, ServerGroup
from apps.groups.services.handover import HANDOVER_KIND
from apps.groups.services.membership import CANCEL_JOIN_KIND, JOIN_KIND, LEAVE_KIND
from apps.groups.services.replication import CATCH_UP_KIND
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import issued_key

pytestmark = pytest.mark.django_db


@pytest.fixture
def browser(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def test_group_is_created_from_a_standalone_server(
    browser: Client, leader: ProtocolInstance
) -> None:
    page = browser.get(reverse("groups:add"))
    assert page.status_code == 200

    response = browser.post(reverse("groups:add"), {"name": "Europe", "leader": leader.pk})

    group = ServerGroup.objects.get()
    assert response.url == reverse("groups:detail", args=[group.pk])  # type: ignore[attr-defined]
    assert group.leader is not None and group.leader.instance == leader


def test_join_warns_only_about_a_server_with_keys(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    clean = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()
    issued_key(joiner)
    with_keys = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "of its own" not in clean
    assert "1 key of its own" in with_keys
    assert browser.get(reverse("groups:list")).status_code == 200


def test_join_starts_an_operation(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    browser.post(reverse("groups:join", args=[group.pk, joiner.pk]))

    assert GroupMember.objects.get(instance=joiner).state == MemberState.JOINING


def test_lagging_member_is_marked(browser: Client, group: ServerGroup) -> None:
    detail = reverse("groups:detail", args=[group.pk])
    assert "lagging</span>" not in browser.get(detail).content.decode()

    group.members.update(lagging_since=timezone.now())

    assert "lagging</span>" in browser.get(detail).content.decode()


def test_lagging_member_shows_why(browser: Client, group: ServerGroup) -> None:
    group.members.fall_behind(LagReason.DRIFT)

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "its files differ from the leading server" in page


def test_relay_of_the_group_is_not_a_candidate(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    join = reverse("groups:join", args=[group.pk, joiner.pk])
    detail = reverse("groups:detail", args=[group.pk])
    assert join in browser.get(detail).content.decode()

    ServerGroup.objects.filter(pk=group.pk).update(relay=joiner.server)

    assert join not in browser.get(detail).content.decode()


def test_joining_server_links_its_operation(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    browser.post(reverse("groups:join", args=[group.pk, joiner.pk]))
    operation = Operation.objects.get(kind=JOIN_KIND)

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "Servers are joining the group" in page
    assert reverse("operations:detail", args=[operation.pk]) in page


def test_key_operations_holding_the_lead_are_linked(
    browser: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    detail = reverse("groups:detail", args=[group.pk])
    assert "until these key operations finish" not in browser.get(detail).content.decode()
    operation = Operation.objects.create(kind="keys.issue", params={"source": leader.source})

    page = browser.get(detail).content.decode()

    assert "until these key operations finish" in page
    assert reverse("operations:detail", args=[operation.pk]) in page


def test_leaving_leader_is_offered_to_make_another_member_leading(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    member = GroupMember.objects.create(
        group=group, instance=joiner, priority=1, state=MemberState.MEMBER
    )
    leader = group.members.get(leading=True)

    response = browser.post(reverse("groups:leave", args=[leader.pk]), follow=True)

    page = response.content.decode()
    assert 'id="leader-leaves"' in page
    assert f"Make {joiner.server.name} leading" in page
    assert reverse("groups:lead", args=[member.pk]) in page
    leader.refresh_from_db()
    assert leader.state == MemberState.MEMBER


def test_make_leading_is_offered_to_a_joined_member_and_starts_an_operation(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    member = GroupMember.objects.create(
        group=group, instance=joiner, priority=1, state=MemberState.MEMBER
    )
    lead = reverse("groups:lead", args=[member.pk])
    assert lead in browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    browser.post(lead)

    assert Operation.objects.get(kind=HANDOVER_KIND).params["member_id"] == member.pk


def test_make_leading_refused_for_a_lagging_member(
    browser: Client, group: ServerGroup, joiner: ProtocolInstance
) -> None:
    member = GroupMember.objects.create(
        group=group,
        instance=joiner,
        priority=1,
        state=MemberState.MEMBER,
        lagging_since=timezone.now(),
    )
    lead = reverse("groups:lead", args=[member.pk])
    assert lead not in browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    response = browser.post(lead, follow=True)

    assert not Operation.objects.filter(kind=HANDOVER_KIND).exists()
    assert "has every key change of the group" in response.content.decode()


@pytest.fixture
def stuck(group: ServerGroup, joiner: ProtocolInstance) -> GroupMember:
    """A member whose leave operation failed."""
    member = GroupMember.objects.create(
        group=group, instance=joiner, priority=1, state=MemberState.LEAVING
    )
    Operation.objects.create(
        kind=LEAVE_KIND, params={"member_id": member.pk}, status=OperationStatus.FAILED
    )
    return member


def test_failed_leave_offers_retry_and_stay(
    browser: Client, group: ServerGroup, stuck: GroupMember
) -> None:
    detail = reverse("groups:detail", args=[group.pk])
    retry = reverse("groups:leave_retry", args=[stuck.pk])
    stay = reverse("groups:stay", args=[stuck.pk])
    page = browser.get(detail).content.decode()
    assert retry in page and stay in page

    browser.post(retry)

    assert Operation.objects.filter(kind=LEAVE_KIND, status=OperationStatus.PENDING).exists()
    page = browser.get(detail).content.decode()
    assert retry not in page and stay not in page


def test_stay_takes_the_member_back_lagging(
    browser: Client, group: ServerGroup, stuck: GroupMember
) -> None:
    browser.post(reverse("groups:stay", args=[stuck.pk]))

    stuck.refresh_from_db()
    assert stuck.state == MemberState.MEMBER
    assert stuck.lagging
    assert Operation.objects.get(kind=CATCH_UP_KIND).params["member_id"] == stuck.pk


@pytest.fixture
def stuck_joining(group: ServerGroup, joiner: ProtocolInstance) -> GroupMember:
    """A member whose join operation failed."""
    member = GroupMember.objects.create(
        group=group, instance=joiner, priority=1, state=MemberState.JOINING
    )
    Operation.objects.create(
        kind=JOIN_KIND, params={"member_id": member.pk}, status=OperationStatus.FAILED
    )
    return member


def test_failed_join_offers_retry_and_taking_out(
    browser: Client, group: ServerGroup, stuck_joining: GroupMember
) -> None:
    detail = reverse("groups:detail", args=[group.pk])
    retry = reverse("groups:join_retry", args=[stuck_joining.pk])
    cancel = reverse("groups:join_cancel", args=[stuck_joining.pk])
    page = browser.get(detail).content.decode()
    assert retry in page and cancel in page
    assert reverse("groups:leave", args=[stuck_joining.pk]) not in page

    browser.post(retry)

    assert Operation.objects.filter(kind=JOIN_KIND, status=OperationStatus.PENDING).exists()
    page = browser.get(detail).content.decode()
    assert retry not in page and cancel not in page


def test_taking_out_a_failed_join_starts_an_operation(
    browser: Client, stuck_joining: GroupMember
) -> None:
    browser.post(reverse("groups:join_cancel", args=[stuck_joining.pk]))

    operation = Operation.objects.get(kind=CANCEL_JOIN_KIND)
    assert operation.params["member_id"] == stuck_joining.pk


def test_edit_saves_the_group_and_says_how_many_keys_moved(
    browser: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    url = reverse("groups:edit", args=[group.pk])
    assert browser.get(url).status_code == 200

    response = browser.post(url, {"name": "Asia", "domain": "vpn.example.com"}, follow=True)

    group.refresh_from_db()
    assert (group.name, group.domain) == ("Asia", "vpn.example.com")
    assert response.redirect_chain[-1][0] == reverse("groups:detail", args=[group.pk])


def test_edit_refuses_a_member_server_as_the_relay(
    browser: Client, group: ServerGroup, leader: ProtocolInstance
) -> None:
    url = reverse("groups:edit", args=[group.pk])

    response = browser.post(url, {"name": "Europe", "relay": leader.server_id})

    assert response.status_code == 200
    group.refresh_from_db()
    assert group.relay is None
