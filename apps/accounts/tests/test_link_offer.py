from datetime import datetime, timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import LinkedAccount, Messenger, User
from apps.accounts.services.link_offer import LinkOffer
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.core.models import SiteSettings

pytestmark = pytest.mark.django_db

TELEGRAM_BOT = {"telegram_bot_token": "123:abc", "telegram_bot_username": "panel_bot"}
DISCORD_APP = {"discord_client_id": "42", "discord_client_secret": "secret"}


def offer(user: User, site: SiteSettings, at: datetime | None = None) -> LinkOffer:
    moment = at or timezone.now()
    return LinkOffer(user, site, clock=lambda: moment)


class TestLinkOffer:
    def test_offers_the_messengers_the_administrator_set_up(self) -> None:
        user = UserFactory()

        assert offer(user, configure()).messengers() == []
        assert offer(user, configure(**TELEGRAM_BOT)).messengers() == [Messenger.TELEGRAM]
        assert offer(user, configure(**DISCORD_APP)).context()["link_offer_names"] == (
            "Telegram or Discord"
        )

    def test_a_user_with_a_linked_messenger_is_not_offered_more(self) -> None:
        user = UserFactory()
        LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="7")

        assert offer(user, configure(**TELEGRAM_BOT)).messengers() == []

    def test_not_now_hides_it_for_30_days_and_the_second_time_for_good(self) -> None:
        site = configure(**TELEGRAM_BOT)
        user = UserFactory()
        now = timezone.now()

        offer(user, site, now).dismiss()

        assert offer(user, site, now + timedelta(days=29)).messengers() == []
        later = now + timedelta(days=31)
        assert offer(user, site, later).messengers() == [Messenger.TELEGRAM]

        offer(user, site, later).dismiss()

        assert offer(user, site, later + timedelta(days=400)).messengers() == []


class TestPages:
    def test_my_access_starts_with_the_offer(self, client: Client) -> None:
        configure(**TELEGRAM_BOT)
        client.force_login(UserFactory())

        page = client.get(reverse("portal:index")).content.decode()

        assert page.index("data-link-offer") < page.index('class="page-header"')
        assert reverse("accounts:telegram-code") in page
        assert reverse("accounts:discord-connect") not in page

    def test_no_offer_without_a_bot(self, client: Client) -> None:
        client.force_login(UserFactory())

        assert "data-link-offer" not in client.get(reverse("portal:index")).content.decode()

    def test_the_chat_widget_offers_it_too(self, client: Client) -> None:
        configure(**DISCORD_APP)
        client.force_login(UserFactory())

        page = client.get(reverse("support:chat"), HTTP_HX_REQUEST="true").content.decode()

        assert 'id="chat-link-offer-title"' in page
        assert reverse("accounts:discord-connect") in page

    def test_not_now_removes_the_card(self, client: Client) -> None:
        configure(**TELEGRAM_BOT)
        user = UserFactory()
        client.force_login(user)
        dismiss = reverse("accounts:link-offer-dismiss")

        assert client.post(dismiss, HTTP_HX_REQUEST="true").content == b""
        assert client.post(dismiss)["Location"] == reverse("portal:index")

        user.refresh_from_db()
        assert user.link_offer_dismissals == 2
        assert "data-link-offer" not in client.get(reverse("portal:index")).content.decode()
