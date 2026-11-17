import pytest
from django.conf import settings
from django.db import connection
from django.utils import translation

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event
from apps.notifications.models import (
    ChannelKind,
    Delivery,
    DeliveryStatus,
    Notification,
    PushSubscription,
    VapidKey,
)
from apps.notifications.services import dispatcher
from apps.notifications.services.browser import (
    BrowserSubscriptions,
    PanelKeys,
    SubscriptionError,
)
from apps.notifications.services.channels import DeliveryError, Message
from apps.notifications.services.delivery import emit, send_browser_test
from apps.notifications.services.webpush import VapidKeys
from apps.notifications.tests.browsers import Browser
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

GONE = DeliveryError("HTTP 410 Gone", retryable=False, status=410)


def subscribe(user: object, browser: Browser) -> None:
    BrowserSubscriptions(user).add(browser.endpoint, browser.p256dh, browser.auth)  # type: ignore[arg-type]


class TestPanelKeys:
    def test_the_pair_is_made_once(self) -> None:
        first = PanelKeys.public()

        assert PanelKeys.public() == first
        assert VapidKey.objects.count() == 1

    def test_the_private_key_is_stored_encrypted(self) -> None:
        keys = PanelKeys.get()

        with connection.cursor() as cursor:
            cursor.execute("SELECT private_key FROM notifications_vapidkey")
            [(stored,)] = cursor.fetchall()
        assert keys.private_text() not in stored
        assert (
            VapidKeys.load(VapidKey.objects.get().private_key).public_text() == PanelKeys.public()
        )


class TestSubscriptions:
    def test_a_browser_subscribes_and_unsubscribes(self) -> None:
        user, browser = UserFactory(), Browser()
        subscriptions = BrowserSubscriptions(user)

        subscribe(user, browser)
        assert subscriptions.has(browser.endpoint)

        subscriptions.remove(browser.endpoint)
        assert not subscriptions.has(browser.endpoint)

    def test_the_browser_moves_to_the_account_that_subscribed_last(self) -> None:
        first, second, browser = UserFactory(), UserFactory(), Browser()

        subscribe(first, browser)
        subscribe(second, browser)

        assert PushSubscription.objects.get().user == second

    def test_another_user_cannot_unsubscribe_a_browser(self) -> None:
        owner, browser = UserFactory(), Browser()
        subscribe(owner, browser)

        BrowserSubscriptions(UserFactory()).remove(browser.endpoint)

        assert BrowserSubscriptions(owner).has(browser.endpoint)

    def test_the_oldest_go_beyond_the_limit(self) -> None:
        user = UserFactory()
        browsers = [Browser(f"https://push.example.com/{n}") for n in range(11)]
        for browser in browsers:
            subscribe(user, browser)

        assert BrowserSubscriptions(user).count() == BrowserSubscriptions.LIMIT
        assert not BrowserSubscriptions(user).has(browsers[0].endpoint)

    @pytest.mark.parametrize(
        "endpoint",
        ["http://push.example.com/1", "https://127.0.0.1/push", "javascript:alert(1)"],
    )
    def test_an_address_the_panel_cannot_send_to_is_refused(self, endpoint: str) -> None:
        with pytest.raises(SubscriptionError):
            subscribe(UserFactory(), Browser(endpoint))

    def test_bad_keys_are_refused(self) -> None:
        browser = Browser()

        with pytest.raises(SubscriptionError):
            BrowserSubscriptions(UserFactory()).add(browser.endpoint, "bad", browser.auth)


class TestUserEvents:
    def test_the_user_gets_their_event_in_every_browser(self, http: FakeHttp) -> None:
        user = UserFactory()
        phone, laptop = (
            Browser("https://push.example.com/phone"),
            Browser("https://push.example.com/laptop"),
        )
        subscribe(user, phone)
        subscribe(user, laptop)

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        pushed = phone.read(http.to(phone.endpoint)[0].body)
        assert pushed["title"] == f"Limit reached: {user.username}"
        assert pushed["url"] == "/portal/usage/"
        assert laptop.read(http.to(laptop.endpoint)[0].body)["tag"] == pushed["tag"]
        browser_delivery = Delivery.objects.get(to_browser=True)
        assert browser_delivery.status == DeliveryStatus.SENT

    def test_the_push_is_signed_and_marked_as_encrypted(self, http: FakeHttp) -> None:
        user, browser = UserFactory(), Browser()
        subscribe(user, browser)

        emit(Event.KEYS_CHANGED, {"user": user.username, "server": "Germany"}, user=user)

        headers = http.to(browser.endpoint)[0].headers
        assert headers["Authorization"].startswith("vapid t=")
        assert headers["Authorization"].endswith(f"k={PanelKeys.public()}")
        assert headers["Content-Encoding"] == "aes128gcm"
        assert headers["TTL"] == "86400"

    def test_a_link_in_the_details_opens_its_page_on_the_panel(self, http: FakeHttp) -> None:
        user, browser = UserFactory(), Browser()
        subscribe(user, browser)
        url = settings.SITE_URL.rstrip("/") + "/support/chat/"

        emit(Event.SUPPORT_ANSWER, {"user": user.username, "url": url}, user=user)

        pushed = browser.read(http.to(browser.endpoint)[0].body)
        assert pushed["url"] == "/support/chat/"
        assert url not in pushed["body"]

    def test_the_user_without_browsers_gets_no_push(self, http: FakeHttp) -> None:
        user = UserFactory()

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert not Delivery.objects.filter(to_browser=True).exists()

    def test_a_user_with_only_a_browser_still_hears_of_it(self, http: FakeHttp) -> None:
        user, browser = UserFactory(email=""), Browser()
        subscribe(user, browser)

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert len(http.to(browser.endpoint)) == 1

    def test_an_answer_sent_to_a_messenger_is_not_pushed(self, http: FakeHttp) -> None:
        user, browser = UserFactory(), Browser()
        subscribe(user, browser)

        emit(Event.CHAT_ANSWER, {"text": "Hi"}, user=user, via="telegram")

        assert http.to(browser.endpoint) == []

    def test_the_message_is_in_the_users_language(
        self, http: FakeHttp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user, browser = UserFactory(language="ru"), Browser()
        subscribe(user, browser)
        seen: list[str] = []
        real = dispatcher.message_of

        def spy(notification: Notification) -> Message:
            seen.append(translation.get_language())
            return real(notification)

        monkeypatch.setattr(dispatcher, "message_of", spy)

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert len(http.to(browser.endpoint)) == 1
        assert seen[-1] == "ru"


class TestDeadSubscriptions:
    @pytest.mark.parametrize("status", [404, 410])
    def test_a_dropped_subscription_is_deleted(self, http: FakeHttp, status: int) -> None:
        user, browser = UserFactory(), Browser()
        subscribe(user, browser)
        http.failing[browser.endpoint] = DeliveryError("gone", retryable=False, status=status)

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert not PushSubscription.objects.exists()
        assert Delivery.objects.get(to_browser=True).status == DeliveryStatus.FAILED

    def test_the_live_browsers_still_get_it(self, http: FakeHttp) -> None:
        user = UserFactory()
        dead, live = (
            Browser("https://push.example.com/dead"),
            Browser("https://push.example.com/live"),
        )
        subscribe(user, dead)
        subscribe(user, live)
        http.failing[dead.endpoint] = GONE

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert len(http.to(live.endpoint)) == 1
        assert list(PushSubscription.objects.values_list("endpoint", flat=True)) == [live.endpoint]
        assert Delivery.objects.get(to_browser=True).status == DeliveryStatus.SENT

    def test_a_failing_push_service_is_tried_again(self, http: FakeHttp) -> None:
        user, browser = UserFactory(), Browser()
        subscribe(user, browser)
        http.failing[browser.endpoint] = DeliveryError("HTTP 503", retryable=True, status=503)

        emit(Event.LIMIT_REACHED, {"user": user.username, "reason": "traffic"}, user=user)

        assert PushSubscription.objects.exists()
        delivery = Delivery.objects.get(to_browser=True)
        assert delivery.attempts == settings.NOTIFICATION_MAX_ATTEMPTS
        assert delivery.status == DeliveryStatus.FAILED


class TestStaffChannel:
    def test_the_administrators_browsers_get_the_routed_events(self, http: FakeHttp) -> None:
        admin, moderator, user = (
            UserFactory(role=Role.ADMIN),
            UserFactory(role=Role.MODERATOR),
            UserFactory(),
        )
        browsers = {
            name: Browser(f"https://push.example.com/{name}")
            for name in ("admin", "moderator", "user")
        }
        for name, member in (("admin", admin), ("moderator", moderator), ("user", user)):
            subscribe(member, browsers[name])
        ChannelFactory(
            kind=ChannelKind.BROWSER, target="", secret="", events=[Event.TICKET_CREATED]
        )

        emit(
            Event.TICKET_CREATED,
            {"user": "anna", "number": 7, "type": "Question", "subject": "Slow"},
        )

        pushed = browsers["admin"].read(http.to(browsers["admin"].endpoint)[0].body)
        assert pushed["url"] == "/support/inbox/7/"
        assert http.to(browsers["moderator"].endpoint) == []
        assert http.to(browsers["user"].endpoint) == []

    def test_without_subscribed_administrators_the_delivery_fails(self, http: FakeHttp) -> None:
        ChannelFactory(
            kind=ChannelKind.BROWSER, target="", secret="", events=[Event.INCIDENT_OPENED]
        )

        emit(Event.INCIDENT_OPENED, {"server": "Germany", "reports": 1})

        delivery = Delivery.objects.get()
        assert delivery.status == DeliveryStatus.FAILED
        assert "no browser" in delivery.error


def test_a_test_push_goes_to_the_users_browsers(http: FakeHttp) -> None:
    user, browser = UserFactory(), Browser()
    subscribe(user, browser)

    send_browser_test(user, "Firefox · Windows")

    pushed = browser.read(http.to(browser.endpoint)[0].body)
    assert "Firefox · Windows" in pushed["body"]
