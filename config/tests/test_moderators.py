"""Two moderators side by side: neither sees nor touches what is in the other's zone."""

from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invite, Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import InviteFactory
from apps.dashboard.models import UserBatch
from apps.dashboard.services.batch import UserBatchActions
from apps.keys.models import AccessKey, Drift, DriftKind, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.services.zone import KeyZone
from apps.keys.tests.conftest import HOST
from apps.operations.models import Operation
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.portal.services.keys import MyKeys
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, TicketType
from apps.support.services.inbox import staff
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_tickets import png
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db


@dataclass
class Side:
    """A moderator with one user, one server, a key of the user there and a request with a
    screenshot."""

    moderator: User
    user: User
    server: Server
    key: AccessKey
    request: Conversation

    @property
    def source(self) -> str:
        return self.key.source

    @property
    def attachment_url(self) -> str:
        attachment = self.request.messages.get().attachments.get()
        return reverse("support:attachment", args=[attachment.pk])


def side(name: str) -> Side:
    moderator = UserFactory(username=f"{name}-moderator", role=Role.MODERATOR)
    user = UserFactory(username=f"{name}-user", manager=moderator)
    server = ServerFactory(name=f"{name}-server")
    moderator.moderated_servers.add(server)
    key = AccessKey.objects.create(
        user=user,
        source=f"awg:{name}",
        name=f"{name}-phone",
        public_key=f"{name}-public",
        status=KeyStatus.ACTIVE,
    )
    request = MyRequests(user).open(TicketType.BUG, f"{name} is broken", png())
    return Side(moderator, user, server, key, request)


@pytest.fixture(autouse=True)
def _media(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def anna(_media: None) -> Side:
    return side("anna")


@pytest.fixture
def boris(_media: None) -> Side:
    return side("boris")


@pytest.fixture(autouse=True)
def _instances(anna: Side, boris: Side) -> Iterator[None]:
    """An instance on each moderator's server."""

    def provider() -> list[Instance]:
        return [
            Instance(s.source, s.server.name, HOST, nullcontext, server_id=s.server.pk)  # type: ignore[arg-type]
            for s in (anna, boris)
        ]

    instances.register(provider)
    yield
    instances.unregister(provider)


def signed_in(client: Client, member: User) -> Client:
    client.force_login(member)
    return client


class TestKeys:
    def test_the_list_shows_only_the_keys_of_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        html = signed_in(client, anna.moderator).get(reverse("keys:list")).content.decode()

        assert anna.key.name in html
        assert boris.key.name not in html

    @pytest.mark.parametrize(
        "call",
        [
            ("get", "keys:detail"),
            ("get", "keys:reveal"),
            ("get", "keys:file", "conf"),
            ("post", "keys:action", "disable"),
            ("post", "keys:assign"),
            ("post", "keys:reset-view"),
        ],
    )
    def test_a_key_of_the_other_zone_is_missing(
        self, client: Client, anna: Side, boris: Side, call: tuple[str, ...]
    ) -> None:
        method, name, *extra = call
        url = reverse(name, args=[boris.key.pk, *extra])

        response = getattr(signed_in(client, anna.moderator), method)(url)

        assert response.status_code == 404
        boris.key.refresh_from_db()
        assert boris.key.status == KeyStatus.ACTIVE

    def test_the_issue_form_offers_only_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        form = signed_in(client, anna.moderator).get(reverse("keys:issue")).context["form"]

        assert list(form.fields["user"].queryset) == [anna.user]
        assert [source for source, _ in form.fields["source"].choices] == [anna.source]

    def test_a_key_cannot_be_issued_into_the_other_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.moderator)

        for user, source in ((boris.user, anna.source), (anna.user, boris.source)):
            data = {"user": user.pk, "source": source, "name": "laptop"}
            assert client.post(reverse("keys:issue"), data).status_code == 200

        assert not AccessKey.objects.filter(name="laptop").exists()

    def test_the_key_quota_stops_issuing(self, client: Client, anna: Side) -> None:
        anna.moderator.key_quota = 1
        anna.moderator.save()
        data = {"user": anna.user.pk, "source": anna.source, "name": "laptop"}

        response = signed_in(client, anna.moderator).post(reverse("keys:issue"), data)

        assert "as many keys as your quota allows" in response.content.decode()
        assert not AccessKey.objects.filter(name="laptop").exists()

    def test_revoked_keys_do_not_count_against_the_quota(self, anna: Side) -> None:
        anna.moderator.key_quota = 1
        anna.key.status = KeyStatus.REVOKED
        anna.key.save()

        assert not KeyZone(anna.moderator).quota_reached()

    def test_differences_with_the_server_stay_in_the_zone(self, anna: Side, boris: Side) -> None:
        for s in (anna, boris):
            Drift.objects.create(
                source=s.source,
                public_key="x",
                kind=DriftKind.UNKNOWN_PEER,
                detected_at=s.key.created_at,
            )

        assert [d.source for d in KeyZone(anna.moderator).drifts()] == [anna.source]

    def test_an_admin_sees_every_key(self, client: Client, anna: Side, boris: Side) -> None:
        admin = UserFactory(role=Role.ADMIN)
        html = signed_in(client, admin).get(reverse("keys:list")).content.decode()

        assert anna.key.name in html
        assert boris.key.name in html


class TestOperations:
    def test_a_moderator_sees_only_their_own_operations(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        mine = Operation.objects.create(kind="keys.issue", created_by=anna.moderator)
        theirs = Operation.objects.create(kind="keys.issue", created_by=boris.moderator)
        signed_in(client, anna.moderator)

        listed = client.get(reverse("operations:list")).context["operations"]

        assert list(listed) == [mine]
        for name in ("operations:detail", "operations:progress"):
            assert client.get(reverse(name, args=[theirs.pk])).status_code == 404
        assert client.post(reverse("operations:retry", args=[theirs.pk])).status_code == 404


class TestSupport:
    def test_the_inbox_shows_only_the_requests_of_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.moderator)

        listed = client.get(reverse("support:inbox")).context["object_list"]
        other = reverse("support:inbox-conversation", args=[boris.request.pk])

        assert list(listed) == [anna.request]
        assert client.get(other).status_code == 404

    def test_attachments_stay_in_the_zone(self, client: Client, anna: Side, boris: Side) -> None:
        signed_in(client, anna.moderator)

        assert client.get(anna.attachment_url).status_code == 200
        assert client.get(boris.attachment_url).status_code == 403

    def test_an_assignment_does_not_reach_outside_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        Conversation.objects.filter(pk=boris.request.pk).update(assignee=anna.moderator)

        assert signed_in(client, anna.moderator).get(boris.attachment_url).status_code == 403

    def test_a_request_is_assigned_only_to_its_users_moderator(
        self, anna: Side, boris: Side
    ) -> None:
        admin = UserFactory(role=Role.ADMIN)

        assert set(staff(anna.user)) == {admin, anna.moderator}


class TestDashboard:
    def test_the_server_cards_are_those_of_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        html = signed_in(client, anna.moderator).get(reverse("dashboard:index")).content.decode()

        assert anna.server.name in html
        assert boris.server.name not in html


class TestUsers:
    def test_the_list_shows_only_the_users_of_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        rows = signed_in(client, anna.moderator).get(reverse("dashboard:users")).context["rows"]

        assert [row.user for row in rows] == [anna.user]

    @pytest.mark.parametrize(
        "call",
        [
            ("get", "dashboard:user"),
            ("get", "plans:user"),
            ("post", "plans:user"),
            ("post", "dashboard:user-link", "telegram"),
            ("post", "dashboard:user-unlink", "telegram"),
            ("post", "dashboard:user-link-code", "telegram"),
        ],
    )
    def test_a_user_of_the_other_zone_is_missing(
        self, client: Client, anna: Side, boris: Side, call: tuple[str, ...]
    ) -> None:
        method, name, *extra = call
        url = reverse(name, args=[boris.user.pk, *extra])

        response = getattr(signed_in(client, anna.moderator), method)(url)

        assert response.status_code == 404
        assert not UserLimits.objects.filter(user=boris.user).exists()

    def test_the_card_and_the_plan_of_their_own_user_open(self, client: Client, anna: Side) -> None:
        signed_in(client, anna.moderator)

        assert client.get(reverse("dashboard:user", args=[anna.user.pk])).status_code == 200
        assert client.get(reverse("plans:user", args=[anna.user.pk])).status_code == 200

    def test_a_batch_takes_only_users_of_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        plan = PlanFactory()
        signed_in(client, anna.moderator)
        data = {"action": "change_plan", "plan": plan.pk}

        client.post(reverse("dashboard:users-batch"), {**data, "users": [boris.user.pk]})
        assert not UserBatch.objects.exists()

        client.post(reverse("dashboard:users-batch"), {**data, "users": [anna.user.pk]})
        assert UserLimits.objects.get(user=anna.user).plan == plan

    def test_a_batch_of_the_other_moderator_is_missing(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        batch = UserBatch.objects.create(action="disable", created_by=boris.moderator)
        signed_in(client, anna.moderator)

        for name in ("dashboard:batch", "dashboard:batch-progress"):
            assert client.get(reverse(name, args=[batch.pk])).status_code == 404

    def test_a_batch_turns_off_only_the_keys_on_the_zone_servers(
        self, anna: Side, boris: Side, django_capture_on_commit_callbacks: Any
    ) -> None:
        AccessKey.objects.create(
            user=anna.user,
            source=boris.source,
            name="elsewhere",
            public_key="elsewhere",
            status=KeyStatus.ACTIVE,
        )

        with django_capture_on_commit_callbacks():
            batch = UserBatchActions(anna.moderator).disable([anna.user])

        operations = batch.items.get().operations.all()
        assert [op.params["key_id"] for op in operations] == [anna.key.pk]


def applicant() -> dict[str, str]:
    password = "a long password 42"
    return {
        "username": "newcomer",
        "email": "newcomer@example.com",
        "password1": password,
        "password2": password,
    }


class TestInvites:
    def test_a_user_who_comes_by_a_moderators_invite_is_in_their_zone(
        self, client: Client, anna: Side
    ) -> None:
        signed_in(client, anna.moderator).post(
            reverse("accounts:invite-add"), {"plan": PlanFactory().pk, "max_uses": 3}
        )
        invite = Invite.objects.get()
        client.logout()

        client.post(reverse("accounts:invite", args=[invite.code]), applicant())

        assert invite.created_by == anna.moderator
        assert User.objects.get(username="newcomer").manager == anna.moderator

    def test_the_user_quota_closes_the_moderators_invites(self, client: Client, anna: Side) -> None:
        anna.moderator.user_quota = 1
        anna.moderator.save()
        invite = InviteFactory(created_by=anna.moderator, max_uses=3)

        response = client.post(reverse("accounts:invite", args=[invite.code]), applicant())

        assert "cannot take new users now" in response.content.decode()
        assert not User.objects.filter(username="newcomer").exists()
        invite.refresh_from_db()
        assert invite.uses == 0

    def test_an_admins_invite_gives_no_moderator(self, client: Client) -> None:
        invite = InviteFactory(created_by=UserFactory(role=Role.ADMIN))

        client.post(reverse("accounts:invite", args=[invite.code]), applicant())

        assert User.objects.get(username="newcomer").manager is None

    def test_a_moderator_sees_and_deletes_only_their_own_invites(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        own = InviteFactory(created_by=anna.moderator)
        other = InviteFactory(created_by=boris.moderator)
        signed_in(client, anna.moderator)

        page = client.get(reverse("accounts:registrations"))
        deleted = client.post(reverse("accounts:invite-delete", args=[other.pk]))

        assert [invite for invite, _link in page.context["invites"]] == [own]
        assert "waiting" not in page.context
        assert deleted.status_code == 404
        assert Invite.objects.filter(pk=other.pk).exists()

    def test_the_queue_stays_with_the_administrators(self, client: Client, anna: Side) -> None:
        waiting = UserFactory(status=Status.PENDING)
        signed_in(client, anna.moderator)

        for name in ("accounts:registration-approve", "accounts:registration-reject"):
            assert client.post(reverse(name, args=[waiting.pk])).status_code == 403
        waiting.refresh_from_db()
        assert waiting.status == Status.PENDING


class TestPortal:
    def test_the_moderators_key_quota_stops_issuing_in_the_portal(self, anna: Side) -> None:
        UserLimitsFactory(
            user=anna.user, plan=PlanFactory(max_keys=5), plan_assigned_at=timezone.now()
        )
        assert MyKeys(anna.user).can_get_key()

        anna.moderator.key_quota = 1
        anna.moderator.save()

        assert not MyKeys(anna.user).can_get_key()

    def test_a_user_without_a_moderator_has_only_the_plan_limit(self, anna: Side) -> None:
        anna.moderator.key_quota = 0
        anna.moderator.save()
        loner = UserFactory()
        UserLimitsFactory(user=loner, plan=PlanFactory(max_keys=5), plan_assigned_at=timezone.now())

        assert MyKeys(loner).can_get_key()


class TestStats:
    def test_the_statistics_cover_only_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        hour = timezone.now().replace(minute=0, second=0, microsecond=0)
        for s, used in ((anna, 10), (boris, 1000)):
            TrafficHourly.objects.create(
                source=s.source, public_key=s.key.public_key, hour=hour, rx_bytes=used, tx_bytes=0
            )
        signed_in(client, anna.moderator)

        page = client.get(reverse("dashboard:stats"), {"period": "day", "user": str(boris.user.pk)})
        export = client.get(reverse("dashboard:stats-export")).content.decode()

        assert page.status_code == 200
        assert page.context["received"] == 10
        assert list(page.context["form"].fields["server"].queryset) == [anna.server]
        assert page.context["storage"] == []
        assert anna.key.name in export
        assert boris.key.name not in export
