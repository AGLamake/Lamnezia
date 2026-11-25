"""Notification letters: the plain text as before and an HTML version in the panel's frame."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from django.core import mail

from apps.accounts.models import NotifyVia
from apps.accounts.services.link_offer import LetterOffer
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.letters import html_version
from apps.accounts.tests.registration import configure as configure_site
from apps.notifications.events import Event
from apps.notifications.models import ChannelKind
from apps.notifications.services.channels import EmailChannel, HttpClient, Message, build_channel
from apps.notifications.services.delivery import emit
from apps.notifications.tests.factories import ChannelFactory

pytestmark = pytest.mark.django_db

PAGE = "https://panel.example.com/support/inbox/7/"
OFFER = LetterOffer("Telegram", "https://panel.example.com/accounts/messengers/")
MESSAGE = Message(
    uid="0b8f",
    event="ticket_created",
    created_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    title="New ticket #7",
    text=f"Alice wrote:\nThe key does not work.\n{PAGE}",
    data={"number": 7, "url": PAGE},
)


def sent(message: Message) -> mail.EmailMessage:
    EmailChannel(["alice@example.com"]).send(message)
    [letter] = mail.outbox
    return letter


def test_the_text_part_is_the_message_text() -> None:
    letter = sent(MESSAGE)

    assert letter.subject == "New ticket #7"
    assert letter.body == MESSAGE.text


def test_the_html_part_has_the_title_the_text_and_the_link_as_a_button() -> None:
    html = html_version(sent(MESSAGE))

    assert '<html lang="en">' in html
    assert "New ticket #7</h1>" in html
    assert "Alice wrote:<br>The key does not work.</p>" in html
    assert f'<a href="{PAGE}"' in html
    assert "Open in the panel</a>" in html


def test_without_a_link_there_is_no_button() -> None:
    message = replace(MESSAGE, text="No answer.", data={"server": "Germany"})

    html = html_version(sent(message))

    assert "No answer.</p>" in html
    assert "Open in the panel" not in html


def test_the_text_is_escaped_in_html() -> None:
    message = replace(MESSAGE, text="<script>alert(1)</script>", data={})

    html = html_version(sent(message))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_offer_to_link_a_messenger_ends_the_text_and_is_a_block_with_a_button() -> None:
    letter = sent(replace(MESSAGE, offer=OFFER))

    assert letter.body == (
        f"{MESSAGE.text}\n\nNotifications can also come to Telegram. "
        f"Link it in your profile: {OFFER.url}"
    )
    html = html_version(letter)
    assert "Notifications can also come to Telegram. Link it in your profile.</p>" in html
    assert f'<a href="{OFFER.url}"' in html
    assert "Link</a>" in html


def test_a_letter_to_a_staff_email_channel_has_the_html_part_too() -> None:
    channel = ChannelFactory(kind=ChannelKind.EMAIL, target="ops@example.com, sre@example.com")

    build_channel(channel, HttpClient(timeout=1)).send(MESSAGE)

    [letter] = mail.outbox
    assert letter.to == ["ops@example.com", "sre@example.com"]
    html = html_version(letter)
    assert f'<a href="{PAGE}"' in html
    assert "Notification settings" not in html
    assert "#notifications" not in letter.body


def test_the_link_to_the_notification_settings_ends_the_text_and_the_frame() -> None:
    url = "https://panel.example.com/accounts/profile/#notifications"

    letter = sent(replace(MESSAGE, settings_url=url))

    assert letter.body == f"{MESSAGE.text}\n\nNotification settings: {url}"
    assert f'<a href="{url}"' in html_version(letter)


@pytest.mark.usefixtures("commit")
def test_a_letter_to_the_user_links_to_their_notification_settings() -> None:
    user = UserFactory(email="anna@example.com", notify_via=NotifyVia.EMAIL)

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-12-01"}, user=user)

    [letter] = mail.outbox
    assert "/accounts/profile/#notifications" in letter.body
    html = html_version(letter)
    assert '/accounts/profile/#notifications" style=' in html
    assert ">Notification settings</a>" in html


@pytest.mark.usefixtures("commit")
def test_a_letter_to_the_user_is_in_their_language_and_offers_a_messenger() -> None:
    configure_site(telegram_bot_token="999:panel", telegram_bot_username="panel_bot")
    user = UserFactory(email="anna@example.com", notify_via=NotifyVia.EMAIL, language="ru")

    emit(Event.ACCESS_EXPIRING, {"user": user.username, "until": "2026-12-01"}, user=user)

    [letter] = mail.outbox
    html = html_version(letter)
    assert '<html lang="ru">' in html
    assert 'href="http' in html and '/accounts/messengers/"' in html
    assert "/accounts/messengers/" in letter.body
