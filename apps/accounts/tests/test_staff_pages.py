"""The «Staff» section and the «Staff» blocks: who makes whom, what may be given, and links."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Management, Role, User
from apps.accounts.services.access import Access, Level
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.models import BatchAction, Outcome, UserBatch
from apps.plans.tests.factories import PlanFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

_PASSWORD = "Long-enough-pass-42"


def _create(client: Client, username: str, role: str, **extra: object) -> User | None:
    client.post(
        reverse("accounts:staff-add"),
        {
            "username": username,
            "email": f"{username}@example.com",
            "password1": _PASSWORD,
            "password2": _PASSWORD,
            "role": role,
            **extra,
        },
    )
    return User.objects.filter(username=username).first()


def test_a_new_member_may_be_asked_to_change_the_password(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    page = client.get(reverse("accounts:staff-add")).content.decode()
    asked = _create(client, "asked", Role.OWNER, must_change_password="on")
    trusted = _create(client, "trusted", Role.OWNER)

    assert 'name="must_change_password"' in page
    assert asked is not None
    assert asked.must_change_password
    assert trusted is not None
    assert not trusted.must_change_password


def _edit(client: Client, member: User, **data: object) -> int:
    response = client.post(reverse("accounts:staff-edit", args=[member.pk]), data)
    return response.status_code


def test_the_administrator_makes_an_owner_and_a_moderator_and_sets_them_up(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    client.force_login(admin)
    server, plan = ServerFactory(), PlanFactory()

    owner = _create(client, "owner", Role.OWNER)
    moderator = _create(client, "moderator", Role.MODERATOR)
    assert owner is not None and owner.role == Role.OWNER
    assert moderator is not None and moderator.created_by == admin

    _edit(
        client,
        moderator,
        user_quota=5,
        key_quota=10,
        servers=[server.pk],
        plans=[plan.pk],
        access_servers=Level.VIEW.value,
    )
    moderator.refresh_from_db()
    assert (moderator.user_quota, moderator.key_quota) == (5, 10)
    assert list(moderator.moderated_servers.all()) == [server]
    assert list(moderator.staffed_plans.all()) == [plan]
    assert Access(moderator).level("servers") == Level.VIEW

    client.post(reverse("accounts:staff-role", args=[owner.pk]), {"role": Role.MODERATOR})
    owner.refresh_from_db()
    assert owner.role == Role.MODERATOR


def test_an_owner_gives_only_their_own_and_no_more_than_they_have(client: Client) -> None:
    owner = UserFactory(role=Role.OWNER, access={"servers": "none"})
    client.force_login(owner)
    mine, foreign = ServerFactory(), ServerFactory()
    mine.moderators.add(owner)
    my_plan, foreign_plan = PlanFactory(), PlanFactory()
    my_plan.staff.add(owner)

    assert _create(client, "boss", Role.OWNER) is None
    moderator = _create(client, "helper", Role.MODERATOR)
    assert moderator is not None and moderator.created_by == owner

    page = client.get(reverse("accounts:staff-edit", args=[moderator.pk]))
    form = page.context["form"]
    assert list(form.fields["servers"].queryset) == [mine]
    assert list(form.fields["plans"].queryset) == [my_plan]
    assert "access_servers" not in form.fields

    assert _edit(client, moderator, servers=[foreign.pk], plans=[foreign_plan.pk]) == 200
    assert not moderator.moderated_servers.exists()
    assert not moderator.staffed_plans.exists()

    _edit(client, moderator, servers=[mine.pk], plans=[my_plan.pk], access_servers=1)
    assert list(moderator.moderated_servers.all()) == [mine]
    moderator.refresh_from_db()
    assert Access(moderator).level("servers") == Level.NONE

    stranger = UserFactory(role=Role.MODERATOR)
    edit_url = reverse("accounts:staff-edit", args=[stranger.pk])
    assert client.get(edit_url).status_code == 404
    foreign_user = UserFactory()
    link = reverse("accounts:staff-block-add", args=["user", foreign_user.pk])
    assert client.post(link, {"member": moderator.pk}).status_code == 404
    assert not Management.objects.filter(user=foreign_user).exists()


def test_a_link_from_the_plan_is_marked_and_stays(client: Client) -> None:
    admin = UserFactory(role=Role.ADMIN)
    client.force_login(admin)
    from_plan, by_hand = UserFactory(role=Role.MODERATOR), UserFactory(role=Role.MODERATOR)
    user = UserFactory()
    Management.objects.create(user=user, member=from_plan, via_plan=True)
    Management.objects.create(user=user, member=by_hand)

    page = client.get(reverse("dashboard:user", args=[user.pk]))
    rows = {row.member: row.via_plan for row in page.context["staff_block"].rows}
    assert rows == {from_plan: True, by_hand: False}
    assert "from the plan" in page.content.decode()

    remove = reverse("accounts:staff-block-remove", args=["user", user.pk])
    client.post(remove, {"member": from_plan.pk})
    client.post(remove, {"member": by_hand.pk})
    assert list(user.managers.all()) == [from_plan]


def test_the_server_and_plan_blocks_add_and_remove_staff(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    moderator = UserFactory(role=Role.MODERATOR)
    server, plan = ServerFactory(), PlanFactory()

    client.post(
        reverse("accounts:staff-block-add", args=["server", server.pk]), {"member": moderator.pk}
    )
    client.post(
        reverse("accounts:staff-block-add", args=["plan", plan.pk]), {"member": moderator.pk}
    )
    assert list(server.moderators.all()) == [moderator]
    assert list(plan.staff.all()) == [moderator]

    client.post(
        reverse("accounts:staff-block-remove", args=["server", server.pk]), {"member": moderator.pk}
    )
    assert not server.moderators.exists()


def test_the_bulk_link_keeps_to_the_quota(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    moderator = UserFactory(role=Role.MODERATOR, user_quota=1)
    first, second = UserFactory(username="a-first"), UserFactory(username="b-second")

    response = client.post(
        reverse("dashboard:users-batch"),
        {"users": [first.pk, second.pk], "action": BatchAction.LINK_STAFF, "member": moderator.pk},
    )

    batch = UserBatch.objects.get()
    assert response.url == reverse("dashboard:batch", args=[batch.pk])  # type: ignore[attr-defined]
    outcomes = {item.user: item.outcome for item in batch.items.all()}
    assert outcomes == {first: Outcome.DONE, second: Outcome.SKIPPED}
    assert list(moderator.managed_users.all()) == [first]


def test_a_moderator_has_no_staff_section(client: Client) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    client.force_login(moderator)
    assert client.get(reverse("accounts:staff")).status_code == 403
    page = client.get(reverse("dashboard:users"))
    assert BatchAction.LINK_STAFF not in dict(page.context["actions"])
