import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import LinkedAccount, LinkToken, Messenger, NotifyVia, Role, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.accounts.tests.test_linked_pages import DISCORD_APP, TELEGRAM_BOT
from apps.audit.models import AuditEvent
from apps.dashboard.tests.test_users_views import listed

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN, username="admin"))
    return client


def link(user: User, messenger: Messenger, external_id: str, username: str = "") -> LinkedAccount:
    return LinkedAccount.objects.create(
        user=user, messenger=messenger, external_id=external_id, username=username
    )


def url(name: str, user: User, messenger: str = Messenger.TELEGRAM) -> str:
    return reverse(f"dashboard:{name}", args=[user.pk, messenger])


class TestList:
    def test_shows_the_linked_accounts(self, admin_client: Client) -> None:
        link(UserFactory(username="alice"), Messenger.TELEGRAM, "1001", "alice_tg")

        page = admin_client.get(reverse("dashboard:users")).content.decode()

        assert "alice_tg" in page

    def test_filters_by_messenger(self, admin_client: Client) -> None:
        link(UserFactory(username="alice"), Messenger.TELEGRAM, "1001")
        link(UserFactory(username="bob"), Messenger.DISCORD, "2002")
        UserFactory(username="carol")

        assert listed(admin_client, messenger="telegram") == ["alice"]
        assert listed(admin_client, messenger="discord") == ["bob"]
        assert listed(admin_client, messenger="none") == ["admin", "carol"]

    def test_search_finds_the_messenger_name_and_id(self, admin_client: Client) -> None:
        alice = UserFactory(username="alice")
        link(alice, Messenger.TELEGRAM, "1001", "rabbit")
        link(alice, Messenger.DISCORD, "2002", "rabbit_dc")
        UserFactory(username="bob")

        assert listed(admin_client, search="@rabbit") == ["alice"]
        assert listed(admin_client, search="2002") == ["alice"]
        assert listed(admin_client, search="200") == []


class TestCard:
    def test_shows_linked_and_free_messengers(self, admin_client: Client) -> None:
        user = UserFactory()
        link(user, Messenger.TELEGRAM, "1001", "alice_tg")

        response = admin_client.get(reverse("dashboard:user", args=[user.pk]))

        page = response.content.decode()
        assert "alice_tg" in page
        assert url("user-unlink", user) in page
        assert url("user-link", user, Messenger.DISCORD) in page
        assert url("user-link-code", user, Messenger.DISCORD) not in page

    def test_offers_a_link_code_when_the_bot_is_set_up(self, admin_client: Client) -> None:
        configure(**TELEGRAM_BOT, **DISCORD_APP)
        user = UserFactory()

        page = admin_client.get(reverse("dashboard:user", args=[user.pk])).content.decode()

        assert url("user-link-code", user) in page
        assert url("user-link-code", user, Messenger.DISCORD) in page


class TestLinkById:
    def test_links_the_account(self, admin_client: Client) -> None:
        user = UserFactory()

        response = admin_client.post(
            url("user-link", user), {"external_id": "1001", "username": "@alice_tg"}
        )

        assert response.status_code == 302
        account = LinkedAccount.objects.get(user=user)
        assert (account.messenger, account.external_id, account.username) == (
            Messenger.TELEGRAM,
            "1001",
            "alice_tg",
        )
        event = AuditEvent.objects.get(action="accounts.link")
        assert event.data == {"messenger": "telegram"}

    def test_refuses_an_account_of_another_user(self, admin_client: Client) -> None:
        link(UserFactory(), Messenger.TELEGRAM, "1001")
        user = UserFactory()

        admin_client.post(url("user-link", user), {"external_id": "1001"})

        assert not LinkedAccount.objects.filter(user=user).exists()
        assert not AuditEvent.objects.filter(action="accounts.link").exists()

    def test_refuses_an_id_that_is_not_a_number(self, admin_client: Client) -> None:
        user = UserFactory()

        admin_client.post(url("user-link", user), {"external_id": "alice"})

        assert not LinkedAccount.objects.exists()

    def test_unknown_messenger_is_not_found(self, admin_client: Client) -> None:
        user = UserFactory()

        response = admin_client.post(url("user-link", user, "icq"), {"external_id": "1"})

        assert response.status_code == 404


def test_unlink_removes_the_account(admin_client: Client) -> None:
    user = UserFactory(notify_via=NotifyVia.TELEGRAM)
    link(user, Messenger.TELEGRAM, "1001")

    response = admin_client.post(url("user-unlink", user))

    assert response.status_code == 302
    assert not LinkedAccount.objects.exists()
    user.refresh_from_db()
    assert user.notify_via == NotifyVia.EMAIL
    assert AuditEvent.objects.filter(action="accounts.unlink").exists()


class TestLinkCode:
    def test_telegram_gets_a_deep_link(self, admin_client: Client) -> None:
        configure(**TELEGRAM_BOT)
        user = UserFactory()

        response = admin_client.post(url("user-link-code", user))

        assert response.status_code == 200
        assert response.context["invitation"].startswith("https://t.me/panel_bot?start=")
        assert LinkToken.objects.filter(user=user, messenger=Messenger.TELEGRAM).exists()
        assert AuditEvent.objects.filter(action="accounts.link_code").exists()

    def test_discord_gets_a_bot_command(self, admin_client: Client) -> None:
        configure(**DISCORD_APP)
        user = UserFactory()

        response = admin_client.post(url("user-link-code", user, Messenger.DISCORD))

        assert response.context["invitation"].startswith("/link ")

    def test_is_not_found_without_the_bot(self, admin_client: Client) -> None:
        user = UserFactory()

        assert admin_client.post(url("user-link-code", user)).status_code == 404


@pytest.mark.parametrize("name", ["user-link", "user-unlink", "user-link-code"])
def test_is_for_the_staff_only(client: Client, name: str) -> None:
    configure(**TELEGRAM_BOT)
    user = UserFactory(role=Role.USER)
    client.force_login(user)

    response = client.post(url(name, user), {"external_id": "1001"})

    assert response.status_code == 403
    assert not LinkedAccount.objects.exists()
    assert not LinkToken.objects.exists()
