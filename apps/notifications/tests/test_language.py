"""Notifications in the language of who reads them, and chat messages joined into one."""

from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone, translation

# Under another name: makemessages must not take the test's strings into the app catalog.
from django.utils.translation import gettext_lazy as lazy

from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event, EventType, english
from apps.notifications.models import Delivery, DeliveryStatus, Notification
from apps.notifications.services import dispatcher
from apps.notifications.services.channels import Message
from apps.notifications.services.delivery import emit
from apps.notifications.services.dispatcher import Dispatcher
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def languages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The language active as each message is written."""
    seen: list[str] = []
    real = dispatcher.message_of

    def spy(notification: Notification) -> Message:
        seen.append(translation.get_language())
        return real(notification)

    monkeypatch.setattr(dispatcher, "message_of", spy)
    return seen


def keys_changed(user: Any) -> None:
    emit(Event.KEYS_CHANGED, {"user": user.username, "server": "Germany"}, user=user)


@pytest.mark.usefixtures("commit")
class TestLanguage:
    def test_each_user_reads_the_same_event_in_their_language(
        self, http: FakeHttp, languages: list[str]
    ) -> None:
        keys_changed(UserFactory(email="anna@example.com", language="ru"))
        keys_changed(UserFactory(email="bob@example.com", language="en"))

        assert languages == ["ru", "en"]
        assert [letter.to for letter in mail.outbox] == [["anna@example.com"], ["bob@example.com"]]

    @pytest.mark.parametrize("site", ["ru", "en"])
    def test_without_a_language_of_their_own_a_user_reads_the_site_language(
        self, site: str, http: FakeHttp, languages: list[str], settings: Any
    ) -> None:
        settings.LANGUAGE_CODE = site

        keys_changed(UserFactory(email="anna@example.com", language=""))

        assert languages == [site]

    def test_a_channel_reads_the_site_language_whoever_the_event_is_about(
        self, http: FakeHttp, languages: list[str], settings: Any
    ) -> None:
        settings.LANGUAGE_CODE = "ru"
        ChannelFactory(events=[Event.KEYS_CHANGED])

        keys_changed(UserFactory(email="bob@example.com", language="en"))

        # The channel first, then the user.
        assert languages == ["ru", "en"]

    def test_the_language_of_the_sender_does_not_leak_into_the_message(
        self, http: FakeHttp, languages: list[str]
    ) -> None:
        user = UserFactory(email="anna@example.com", language="ru")

        with translation.override("en"):
            keys_changed(user)

        assert languages == ["ru"]


def test_a_translated_detail_reads_in_the_active_language() -> None:
    kind = EventType(lazy("Test"), lazy("{status}"), lazy("{status}"))
    status = EventType(kind.label, kind.title, kind.text, translated=("status",))
    # Django's own catalog is always compiled: its words show the language in use.
    with translation.override("ru"):
        data = {"status": english(lazy("Yes"))}

    with translation.override("ru"):
        assert status.render(data) == ("Да", "Да")
        assert kind.render(data) == ("Yes", "Yes")
    assert data == {"status": "Yes"}


class TestChatSeries:
    """Without `commit` the deliveries wait, as they do through the join window."""

    def say(self, user: Any, text: str) -> Notification | None:
        return emit(Event.CHAT_MESSAGE, {"user": user.username, "text": text}, user=user)

    def test_a_series_of_messages_is_one_notification(self, http: FakeHttp) -> None:
        ChannelFactory(events=[Event.CHAT_MESSAGE])
        user = UserFactory()

        for text in ("Hello", "The key does not import", "It says the config is wrong"):
            self.say(user, text)

        notification = Notification.objects.get()
        assert notification.data == {
            "user": user.username,
            "text": "Hello\nThe key does not import\nIt says the config is wrong",
            "count": 3,
        }
        assert Delivery.objects.count() == 1

    def test_the_joined_notification_is_sent_once_with_every_message(self, http: FakeHttp) -> None:
        ChannelFactory(events=[Event.CHAT_MESSAGE])
        user = UserFactory()
        self.say(user, "Hello")
        self.say(user, "Anyone there?")

        Dispatcher.from_settings().deliver(Delivery.objects.get().pk)

        [post] = http.posts
        assert b"Hello\\nAnyone there?" in post.body

    def test_the_deliveries_wait_out_the_window(
        self, http: FakeHttp, commit: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "apps.notifications.tasks.deliver.apply_async",
            lambda args, **options: queued.append(options),
        )
        ChannelFactory(events=[Event.CHAT_MESSAGE])

        self.say(UserFactory(), "Hello")

        window = int(settings.NOTIFICATION_JOIN_WINDOW.total_seconds())
        assert queued == [{"countdown": window}]

    def test_a_message_after_the_notification_left_starts_a_new_one(self, http: FakeHttp) -> None:
        ChannelFactory(events=[Event.CHAT_MESSAGE])
        user = UserFactory()
        first = self.say(user, "Hello")
        Delivery.objects.update(status=DeliveryStatus.SENT)

        second = self.say(user, "Hello again")

        assert first is not None and second is not None
        assert first.pk != second.pk

    def test_a_message_after_the_window_starts_a_new_one(self, http: FakeHttp) -> None:
        ChannelFactory(events=[Event.CHAT_MESSAGE])
        user = UserFactory()
        self.say(user, "Hello")
        long_ago = timezone.now() - settings.NOTIFICATION_JOIN_WINDOW - timedelta(seconds=1)
        Notification.objects.update(created_at=long_ago)

        self.say(user, "Hello again")

        assert Notification.objects.count() == 2

    def test_the_messages_of_two_users_are_not_joined(self, http: FakeHttp) -> None:
        ChannelFactory(events=[Event.CHAT_MESSAGE])

        self.say(UserFactory(), "Hello")
        self.say(UserFactory(), "Hello")

        assert Notification.objects.count() == 2

    def test_other_events_are_never_joined(self, http: FakeHttp) -> None:
        user = UserFactory(email="anna@example.com")

        keys_changed(user)
        keys_changed(user)

        assert Notification.objects.count() == 2
