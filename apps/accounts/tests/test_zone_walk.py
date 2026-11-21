"""The whole panel walked by two owners with a moderator each and a user they share: nobody sees
what belongs to the other side, in any section, and nothing of the other side opens."""

from dataclasses import dataclass

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Invite, Role, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.test_section_urls import SECTION_URLS
from apps.plans.models import Plan
from apps.plans.tests.factories import PlanFactory
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.support.models import TicketType
from apps.support.services.tickets import MyRequests

pytestmark = pytest.mark.django_db

# The pages of the sections that need no object: lists, summaries and the forms of new objects,
# whose choices must not offer anything of the other side either.
PAGES = [url for url in SECTION_URLS if "999999" not in url.url and "/x/" not in url.url]
_SHARED = "zoneshared-client"


@dataclass(frozen=True)
class Side:
    """An owner, their moderator and what is theirs; every name starts with the side's mark."""

    mark: str
    owner: User
    moderator: User
    client: User
    server: Server
    plan: Plan
    invite: Invite

    @classmethod
    def build(cls, name: str, shared: User) -> "Side":
        mark = f"zone{name}"
        owner = UserFactory(role=Role.OWNER, username=f"{mark}-owner")
        moderator = UserFactory(
            role=Role.MODERATOR,
            username=f"{mark}-moderator",
            created_by=owner,
            access={"servers": "view", "plans": "view"},
        )
        server = ServerFactory(name=f"{mark}-server")
        server.moderators.add(owner, moderator)
        plan = PlanFactory(name=f"{mark}-plan")
        plan.staff.add(owner, moderator)
        client = UserFactory(username=f"{mark}-client", managers=[owner, moderator])
        shared.managers.add(owner)
        MyRequests(client).open(TicketType.BUG, f"{mark}-request")
        invite = _invite(owner, plan, f"{mark}-invite")
        return cls(mark, owner, moderator, client, server, plan, invite)

    @property
    def staff(self) -> tuple[User, User]:
        return self.owner, self.moderator

    def foreign_urls(self) -> list[tuple[str, str]]:
        """The pages of this side's objects, by method, which the other side must not open."""
        return [
            ("get", reverse("servers:detail", args=[self.server.pk])),
            ("get", reverse("dashboard:user", args=[self.client.pk])),
            ("get", reverse("plans:user", args=[self.client.pk])),
            ("get", reverse("accounts:staff-edit", args=[self.moderator.pk])),
            ("post", reverse("plans:delete", args=[self.plan.pk])),
            ("post", reverse("accounts:invite-delete", args=[self.invite.pk])),
        ]


def _invite(owner: User, plan: Plan, note: str) -> Invite:
    """An invite made on the page, so the audit log has the owner's action too."""
    client = Client()
    client.force_login(owner)
    client.post(reverse("accounts:invite-add"), {"plan": plan.pk, "max_uses": 1, "note": note})
    return Invite.objects.get(note=note)


@pytest.fixture
def sides() -> tuple[Side, Side]:
    shared = UserFactory(username=_SHARED)
    return Side.build("alpha", shared), Side.build("bravo", shared)


def _pages(member: User) -> dict[str, str]:
    client = Client(raise_request_exception=False)
    client.force_login(member)
    return {url.name: client.get(url.url).content.decode() for url in PAGES}


def test_nobody_sees_the_other_side_in_any_section(sides: tuple[Side, Side]) -> None:
    for side, other in (sides, sides[::-1]):
        for member in side.staff:
            seen = {name for name, page in _pages(member).items() if other.mark in page}
            assert not seen, (member.username, sorted(seen))


def test_nothing_of_the_other_side_opens(sides: tuple[Side, Side]) -> None:
    for side, other in (sides, sides[::-1]):
        for member in side.staff:
            client = Client(raise_request_exception=False)
            client.force_login(member)
            for method, url in other.foreign_urls():
                status = getattr(client, method)(url).status_code
                assert status in (403, 404), (member.username, method, url, status)
    assert Plan.objects.count() == Invite.objects.count() == 2


def test_each_side_sees_its_own_and_the_shared_user(sides: tuple[Side, Side]) -> None:
    for side in sides:
        users = _pages(side.owner)["dashboard:users"]
        assert f"{side.mark}-client" in users
        assert _SHARED in users
    admin = _pages(UserFactory(role=Role.ADMIN))
    for side in sides:
        assert f"{side.mark}-client" in admin["dashboard:users"]
        assert f"{side.mark}-server" in admin["servers:list"]
        assert f"{side.mark}-invite" in admin["accounts:registrations"]
