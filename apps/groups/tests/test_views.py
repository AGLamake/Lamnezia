import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.handover import HANDOVER_KIND
from apps.operations.models import Operation
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
