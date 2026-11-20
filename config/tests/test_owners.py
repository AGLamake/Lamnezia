"""Two server administrators side by side: each runs their own servers, plans and users, and
whatever is the other's does not exist for them."""

from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.services.access import Access, Level, NotAllowedError, Section
from apps.accounts.services.two_factor import TwoFactor
from apps.accounts.services.zone import Zone
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.audit.services import journal as audit
from apps.backups.models import ProtocolSnapshot, SnapshotReason
from apps.backups.tests.helpers import awg_instance
from apps.billing.models import Order
from apps.dashboard.services.attention import Attention
from apps.dashboard.services.palette import QuickSearch
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.conftest import HOST
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import Plan, UserLimits
from apps.plans.tests.factories import PlanFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.support.models import Conversation, TicketType
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_tickets import png

pytestmark = pytest.mark.django_db


@dataclass
class Side:
    """An owner with a server running a protocol, a user with a key there, a plan with an order
    and a request with a screenshot."""

    owner: User
    user: User
    server: Server
    instance: ProtocolInstance
    snapshot: ProtocolSnapshot
    key: AccessKey
    plan: Plan
    order: Order
    request: Conversation

    @property
    def source(self) -> str:
        return self.key.source

    @property
    def attachment_url(self) -> str:
        attachment = self.request.messages.get().attachments.get()
        return reverse("support:attachment", args=[attachment.pk])


def side(name: str) -> Side:
    owner = UserFactory(username=f"{name}-owner", role=Role.OWNER)
    user = UserFactory(username=f"{name}-user", managers=[owner])
    server = ServerFactory(name=f"{name}-server")
    owner.moderated_servers.add(server)
    instance = awg_instance(server)
    snapshot = ProtocolSnapshot.objects.create(
        instance=instance, reason=SnapshotReason.GROUP_JOIN, config="[Interface]"
    )
    key = AccessKey.objects.create(
        user=user,
        source=f"awg:{name}",
        name=f"{name}-phone",
        public_key=f"{name}-public",
        status=KeyStatus.ACTIVE,
    )
    plan = PlanFactory(name=f"{name}-plan")
    plan.servers.add(server)
    plan.staff.add(owner)
    order = Order.objects.create(
        user=user, plan=plan, provider="fake", amount=Decimal(100), currency="RUB"
    )
    request = MyRequests(user).open(TicketType.BUG, f"{name} is broken", png())
    return Side(owner, user, server, instance, snapshot, key, plan, order, request)


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


class TestOwnServers:
    def test_an_added_server_is_in_the_owners_staff(self, client: Client, anna: Side) -> None:
        data = {
            "name": "new-server",
            "host": "203.0.113.50",
            "port": 22,
            "username": "root",
            "auth_method": "password",
            "password": "secret",
        }

        response = signed_in(client, anna.owner).post(reverse("servers:add"), data)

        server = Server.objects.get(name="new-server")
        assert response.status_code == 302
        assert list(server.moderators.all()) == [anna.owner]
        assert client.get(reverse("servers:detail", args=[server.pk])).status_code == 200

    def test_the_owner_runs_their_server(self, client: Client, anna: Side) -> None:
        signed_in(client, anna.owner)

        assert client.get(reverse("protocols:install", args=[anna.server.pk])).status_code == 200
        assert client.get(reverse("protocols:settings", args=[anna.instance.pk])).status_code == 200
        deleted = client.post(reverse("backups:delete-snapshot", args=[anna.snapshot.pk]))
        assert deleted.status_code == 302
        assert not ProtocolSnapshot.objects.filter(pk=anna.snapshot.pk).exists()

    def test_the_owner_sees_the_operations_of_their_team(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        moderator.moderated_servers.add(anna.server)
        own = Operation.objects.create(kind="keys.issue", created_by=anna.owner)
        team = Operation.objects.create(kind="keys.issue", created_by=moderator)
        Operation.objects.create(kind="keys.issue", created_by=boris.owner)

        listed = signed_in(client, anna.owner).get(reverse("operations:list"))

        assert set(listed.context["operations"]) == {own, team}

    def test_groups_and_relays_are_the_administrators(self, client: Client, anna: Side) -> None:
        signed_in(client, anna.owner)

        assert client.get(reverse("groups:list")).status_code == 404
        assert "groups:list" not in client.get(reverse("servers:list")).content.decode()


class TestTwoOwners:
    @pytest.mark.parametrize(
        "call",
        [
            ("get", "servers:detail", "server"),
            ("get", "servers:edit", "server"),
            ("post", "servers:check", "server"),
            ("post", "servers:delete", "server"),
            ("post", "servers:leave", "server"),
            ("get", "protocols:install", "server"),
            ("get", "protocols:settings", "instance"),
            ("post", "protocols:remove", "instance"),
            ("post", "backups:restore", "instance"),
            ("post", "backups:restore-snapshot", "snapshot"),
            ("post", "backups:delete-snapshot", "snapshot"),
            ("get", "plans:edit", "plan"),
            ("post", "plans:delete", "plan"),
            ("get", "dashboard:user", "user"),
            ("get", "plans:user", "user"),
            ("get", "keys:detail", "key"),
            ("get", "support:inbox-conversation", "request"),
        ],
    )
    def test_an_object_of_the_other_owner_is_missing(
        self, client: Client, anna: Side, boris: Side, call: tuple[str, str, str]
    ) -> None:
        method, name, obj = call
        url = reverse(name, args=[getattr(boris, obj).pk])

        response = getattr(signed_in(client, anna.owner), method)(url)

        assert response.status_code == 404
        assert Server.objects.filter(pk=boris.server.pk).exists()
        assert Plan.objects.filter(pk=boris.plan.pk).exists()
        assert ProtocolSnapshot.objects.filter(pk=boris.snapshot.pk).exists()

    def test_an_operation_of_the_other_owner_is_missing(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        theirs = Operation.objects.create(kind="keys.issue", created_by=boris.owner)

        response = signed_in(client, anna.owner).get(reverse("operations:detail", args=[theirs.pk]))

        assert response.status_code == 404

    def test_an_attachment_of_the_other_owner_is_closed(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.owner)

        assert client.get(anna.attachment_url).status_code == 200
        assert client.get(boris.attachment_url).status_code == 403

    def test_lists_show_only_their_own(self, client: Client, anna: Side, boris: Side) -> None:
        signed_in(client, anna.owner)

        assert list(client.get(reverse("servers:list")).context["servers"]) == [anna.server]
        assert list(client.get(reverse("plans:list")).context["plans"]) == [anna.plan]
        assert list(client.get(reverse("billing:orders")).context["orders"]) == [anna.order]
        rows = client.get(reverse("dashboard:users")).context["rows"]
        assert [row.user for row in rows] == [anna.user]

    def test_the_other_owners_user_is_not_found_by_name(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.owner)

        response = client.get(reverse("plans:lookup"), {"username": boris.user.username})

        assert response.url == reverse("plans:list")  # type: ignore[attr-defined]

    def test_the_palette_finds_only_their_own(self, anna: Side, boris: Side) -> None:
        search = QuickSearch(anna.owner)

        def found(text: str) -> set[str]:
            return {hit.title for group in search.groups(text) for hit in group.hits}

        assert found("anna") == {anna.server.name, anna.user.username, anna.key.name}
        assert found("boris") == set()

    def test_attention_counts_only_their_own(self, anna: Side, boris: Side) -> None:
        Operation.objects.create(
            kind="keys.issue", created_by=boris.owner, status=OperationStatus.FAILED
        )

        attention = Attention(anna.owner)

        assert attention.servers == [anna.server]
        assert attention.failed_operations == 0

    def test_the_dashboard_and_stats_show_only_their_own(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.owner)

        for name in ("dashboard:index", "dashboard:stats"):
            html = client.get(reverse(name)).content.decode()
            assert boris.server.name not in html
            assert boris.key.name not in html

    def test_the_audit_log_shows_the_owner_and_their_moderators(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        moderator.moderated_servers.add(anna.server)
        for actor in (anna.owner, moderator, boris.owner):
            audit.log("plans.create", actor=actor)

        events = signed_in(client, anna.owner).get(reverse("audit:list")).context["events"]

        assert {event.actor for event in events} == {anna.owner, moderator}
        assert AuditEvent.objects.count() == 3


class TestSharedUser:
    def test_both_owners_see_the_user_and_only_their_keys(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        anna.user.managers.add(boris.owner)
        AccessKey.objects.create(
            user=anna.user,
            source=boris.source,
            name="on-boris",
            public_key="on-boris",
            status=KeyStatus.ACTIVE,
        )

        url = reverse("dashboard:user", args=[anna.user.pk])

        for owner, mine, other in (
            (anna, anna.key.name, "on-boris"),
            (boris, "on-boris", anna.key.name),
        ):
            response = signed_in(client, owner.owner).get(url)
            assert response.status_code == 200
            assert mine in response.content.decode()
            assert other not in response.content.decode()


class TestPlans:
    def test_the_form_offers_only_their_servers(self, client: Client, anna: Side) -> None:
        form = signed_in(client, anna.owner).get(reverse("plans:add")).context["form"]

        assert list(form.fields["servers"].queryset) == [anna.server]
        assert not form.fields["groups"].queryset.exists()

    def test_a_new_plan_is_theirs_and_takes_only_their_servers(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        signed_in(client, anna.owner)
        data = {"name": "fresh", "period_days": 30, "currency": "", "servers": [boris.server.pk]}

        refused = client.post(reverse("plans:add"), data)
        empty = client.post(reverse("plans:add"), {**data, "servers": []})
        client.post(reverse("plans:add"), {**data, "servers": [anna.server.pk]})

        assert refused.status_code == empty.status_code == 200
        plan = Plan.objects.get(name="fresh")
        assert list(plan.servers.all()) == [anna.server]
        assert list(plan.staff.all()) == [anna.owner]

    def test_their_users_are_given_only_their_plans(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        url = reverse("plans:user", args=[anna.user.pk])
        form = signed_in(client, anna.owner).get(url).context["form"]

        assert list(form.fields["plan"].queryset) == [anna.plan]
        client.post(
            reverse("dashboard:users-batch"),
            {"action": "change_plan", "plan": boris.plan.pk, "users": [anna.user.pk]},
        )
        assert not UserLimits.objects.filter(user=anna.user, plan=boris.plan).exists()

    def test_editing_keeps_servers_outside_the_zone(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        anna.plan.servers.add(boris.server)
        data = {"name": "renamed", "period_days": 30, "currency": "", "servers": [anna.server.pk]}

        signed_in(client, anna.owner).post(reverse("plans:edit", args=[anna.plan.pk]), data)

        anna.plan.refresh_from_db()
        assert anna.plan.name == "renamed"
        assert set(anna.plan.servers.all()) == {anna.server, boris.server}


class TestServerStaff:
    def test_a_shared_server_is_left_not_deleted(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        boris.owner.moderated_servers.add(anna.server)
        signed_in(client, anna.owner)

        client.post(reverse("servers:delete", args=[anna.server.pk]))
        assert Server.objects.filter(pk=anna.server.pk).exists()

        client.post(reverse("servers:leave", args=[anna.server.pk]))
        assert list(anna.server.moderators.all()) == [boris.owner]

    def test_the_sole_owner_deletes_a_server_without_protocols(
        self, client: Client, anna: Side
    ) -> None:
        empty = ServerFactory()
        anna.owner.moderated_servers.add(empty)
        signed_in(client, anna.owner)

        client.post(reverse("servers:delete", args=[anna.server.pk]))
        client.post(reverse("servers:delete", args=[empty.pk]))

        assert Server.objects.filter(pk=anna.server.pk).exists()
        assert not Server.objects.filter(pk=empty.pk).exists()
        assert AuditEvent.objects.filter(action="servers.delete").count() == 1

    def test_the_last_owner_cannot_leave(self, client: Client, anna: Side) -> None:
        signed_in(client, anna.owner).post(reverse("servers:leave", args=[anna.server.pk]))

        assert list(anna.server.moderators.all()) == [anna.owner]

    def test_the_administrator_deletes_a_shared_server(
        self, client: Client, anna: Side, boris: Side
    ) -> None:
        empty = ServerFactory()
        for side_ in (anna, boris):
            side_.owner.moderated_servers.add(empty)

        signed_in(client, UserFactory(role=Role.ADMIN)).post(
            reverse("servers:delete", args=[empty.pk])
        )

        assert not Server.objects.filter(pk=empty.pk).exists()


class TestStaffRights:
    def test_an_owner_grants_only_to_their_moderators(self, anna: Side, boris: Side) -> None:
        mine = UserFactory(role=Role.MODERATOR)
        mine.moderated_servers.add(anna.server)
        theirs = UserFactory(role=Role.MODERATOR)
        theirs.moderated_servers.add(boris.server)

        Access(anna.owner).grant(mine, Section.KEYS, Level.VIEW)

        with pytest.raises(NotAllowedError):
            Access(anna.owner).grant(theirs, Section.KEYS, Level.VIEW)
        with pytest.raises(NotAllowedError):
            Access(anna.owner).grant(boris.owner, Section.KEYS, Level.VIEW)
        assert list(Zone(anna.owner).moderators()) == [mine]

    def test_an_owner_signs_in_with_a_code(self, settings: Any, anna: Side) -> None:
        settings.REQUIRE_ADMIN_2FA = True

        assert TwoFactor(anna.owner).required
        assert not TwoFactor(anna.user).required

    def test_django_admin_stays_closed(self, client: Client, anna: Side) -> None:
        response = signed_in(client, anna.owner).get("/admin/")

        assert response.status_code in (302, 403, 404)
