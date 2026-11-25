"""A letter about the end of access offers to link a messenger to a user without one."""

import pytest
from django.core import mail

from apps.accounts.models import LinkedAccount, Messenger, NotifyVia, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure as configure_site
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

TELEGRAM_BOT = {"telegram_bot_token": "999:panel", "telegram_bot_username": "panel_bot"}
OFFER = "Link it in your profile"


def alice() -> User:
    user: User = UserFactory(email="alice@example.com", notify_via=NotifyVia.EMAIL, language="en")
    return user


def letter_text(event: Event, user: User) -> str:
    emit(event, {"user": user.username, "until": "2026-12-01", "reason": "traffic"}, user=user)
    [letter] = mail.outbox
    return str(letter.body)


@pytest.mark.parametrize("event", [Event.ACCESS_EXPIRING, Event.LIMIT_REACHED])
def test_a_user_without_messengers_is_offered_to_link_one(event: Event) -> None:
    configure_site(**TELEGRAM_BOT)

    text = letter_text(event, alice())

    assert "Notifications can also come to Telegram. Link it in your profile:" in text
    assert "Link it in your profile: http://localhost:8000/accounts/messengers/\n" in text


def test_both_bots_are_named() -> None:
    configure_site(**TELEGRAM_BOT, discord_client_id="1", discord_client_secret="secret")

    assert "Telegram or Discord" in letter_text(Event.ACCESS_EXPIRING, alice())


def test_no_offer_to_a_user_with_a_linked_messenger() -> None:
    configure_site(**TELEGRAM_BOT)
    user = alice()
    LinkedAccount.objects.create(user=user, messenger=Messenger.DISCORD, external_id="501")

    assert OFFER not in letter_text(Event.ACCESS_EXPIRING, user)


def test_no_offer_without_a_bot() -> None:
    configure_site()

    assert OFFER not in letter_text(Event.LIMIT_REACHED, alice())


def test_no_offer_in_other_letters() -> None:
    configure_site(**TELEGRAM_BOT)

    emit(Event.KEYS_REVOKED, {"server": "Helsinki"}, user=alice())

    [letter] = mail.outbox
    assert OFFER not in letter.body
