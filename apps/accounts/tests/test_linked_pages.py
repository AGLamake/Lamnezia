import pytest
from django.test import Client

from apps.accounts.models import LinkedAccount, LinkToken, Messenger, NotifyVia
from apps.accounts.services.messengers import DiscordOAuth, MessengerIdentity
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.accounts.tests.test_messengers import BOT_TOKEN, telegram_data
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

PAGE = "/accounts/profile/#messengers"
NOTIFY = "/accounts/profile/notifications/"
TELEGRAM_BOT = {"telegram_bot_token": BOT_TOKEN, "telegram_bot_username": "panel_bot"}
DISCORD_APP = {"discord_client_id": "42", "discord_client_secret": "secret"}


@pytest.fixture
def user_client(client: Client) -> Client:
    client.force_login(UserFactory(username="alice"))
    return client


def linked(username: str = "alice", messenger: Messenger = Messenger.TELEGRAM) -> LinkedAccount:
    user = UserFactory(username=username)
    return LinkedAccount.objects.create(user=user, messenger=messenger, external_id="1001")


class TestSignInWithMessenger:
    def test_is_off_by_default(self, client: Client) -> None:
        configure(**TELEGRAM_BOT, **DISCORD_APP)
        linked()

        page = client.get("/accounts/login/").content.decode()

        assert "oauth.telegram.org" not in page
        assert "/accounts/discord/login/" not in page
        assert client.get("/accounts/telegram/login/", telegram_data()).status_code == 404
        assert client.get("/accounts/discord/login/").status_code == 404
        assert "_auth_user_id" not in client.session

    def test_telegram_signs_in_the_linked_user(self, client: Client) -> None:
        configure(telegram_login=True, **TELEGRAM_BOT)
        account = linked()
        page = client.get("/accounts/login/")
        assert "https://oauth.telegram.org/auth?bot_id=123&amp;origin=http%3A%2F%2Ftestserver" in (
            page.content.decode()
        )
        assert "telegram.org" not in page["Content-Security-Policy"]

        response = client.get("/accounts/telegram/login/", telegram_data())

        assert response["Location"] == "/"
        assert client.session["_auth_user_id"] == str(account.user.pk)
        assert AuditEvent.objects.filter(action="accounts.messenger_login").exists()

    def test_the_return_from_telegram_without_data_is_read_by_the_script(
        self, client: Client
    ) -> None:
        configure(telegram_login=True, **TELEGRAM_BOT)

        page = client.get("/accounts/telegram/login/").content.decode()

        assert "js/telegram-login.js" in page
        assert "_auth_user_id" not in client.session

    def test_telegram_does_not_sign_in_an_unlinked_account(self, client: Client) -> None:
        configure(telegram_login=True, **TELEGRAM_BOT)
        linked()

        response = client.get("/accounts/telegram/login/", telegram_data(id="2002"))

        assert response["Location"] == "/accounts/login/"
        assert "_auth_user_id" not in client.session

    def test_discord_signs_in_the_linked_user(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(discord_login=True, **DISCORD_APP)
        account = linked(messenger=Messenger.DISCORD)
        discord_answers(monkeypatch, "1001")

        state = start_discord(client, "/accounts/discord/login/")
        client.get("/accounts/discord/callback/", {"state": state, "code": "c0de"})

        assert client.session["_auth_user_id"] == str(account.user.pk)


def discord_answers(monkeypatch: pytest.MonkeyPatch, external_id: str) -> None:
    identity = MessengerIdentity(external_id, "bob")
    monkeypatch.setattr(DiscordOAuth, "identity", lambda self, code: identity)


def start_discord(client: Client, url: str) -> str:
    response = client.get(url)
    assert response["Location"].startswith("https://discord.com/oauth2/authorize?")
    return str(client.session["accounts:discord-oauth"]["state"])


class TestLinking:
    def test_telegram_code_is_a_deep_link_to_the_bot(self, user_client: Client) -> None:
        configure(**TELEGRAM_BOT)

        page = user_client.post("/accounts/telegram/code/").content.decode()

        assert "https://t.me/panel_bot?start=" in page
        assert LinkToken.objects.get().messenger == Messenger.TELEGRAM

    def test_no_code_without_the_bot(self, user_client: Client) -> None:
        assert user_client.post("/accounts/telegram/code/").status_code == 404
        assert user_client.post("/accounts/discord/code/").status_code == 404

    def test_discord_code_is_a_bot_command(self, user_client: Client) -> None:
        configure(**DISCORD_APP)

        assert "/link " in user_client.post("/accounts/discord/code/").content.decode()

    def test_discord_oauth_links_the_account(
        self, user_client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(**DISCORD_APP)
        discord_answers(monkeypatch, "555")

        state = start_discord(user_client, "/accounts/discord/connect/")
        response = user_client.get("/accounts/discord/callback/", {"state": state, "code": "c"})

        assert response["Location"] == PAGE
        account = LinkedAccount.objects.get()
        assert (account.user.username, account.external_id) == ("alice", "555")

    def test_discord_callback_with_another_state_links_nothing(
        self, user_client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(**DISCORD_APP)
        discord_answers(monkeypatch, "555")

        start_discord(user_client, "/accounts/discord/connect/")
        user_client.get("/accounts/discord/callback/", {"state": "forged", "code": "c"})

        assert not LinkedAccount.objects.exists()

    def test_discord_account_of_another_user_is_not_linked(
        self, user_client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(**DISCORD_APP)
        linked("bob", Messenger.DISCORD)
        discord_answers(monkeypatch, "1001")

        state = start_discord(user_client, "/accounts/discord/connect/")
        user_client.get("/accounts/discord/callback/", {"state": state, "code": "c"})

        assert LinkedAccount.objects.get().user.username == "bob"

    def test_unlink(self, client: Client) -> None:
        account = linked()
        client.force_login(account.user)

        assert client.post("/accounts/telegram/unlink/")["Location"] == PAGE
        assert not LinkedAccount.objects.exists()


class TestNotifyVia:
    def test_a_linked_messenger_can_be_chosen(self, client: Client) -> None:
        account = linked()
        client.force_login(account.user)

        client.post(NOTIFY, {"notify_via": NotifyVia.TELEGRAM})

        account.user.refresh_from_db()
        assert account.user.notify_via == NotifyVia.TELEGRAM

    def test_an_unlinked_messenger_cannot_be_chosen(self, user_client: Client) -> None:
        response = user_client.post(NOTIFY, {"notify_via": NotifyVia.DISCORD})

        assert response.status_code == 200
        assert response.context["notify_form"].errors

    def test_the_notification_language_can_be_chosen(self, client: Client) -> None:
        account = linked()
        client.force_login(account.user)

        client.post(NOTIFY, {"notify_via": NotifyVia.EMAIL, "language": "en"})

        account.user.refresh_from_db()
        assert account.user.notification_language == "en"
