from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import PushSubscription
from apps.notifications.services.browser import BrowserSubscriptions, PanelKeys
from apps.notifications.tests.browsers import Browser
from apps.notifications.tests.conftest import FakeHttp

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("public_dns")]

FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"


def signed_in(user: User) -> Client:
    client = Client(HTTP_USER_AGENT=FIREFOX)
    client.force_login(user)
    return client


def post(client: Client, action: str, data: dict[str, str]) -> Any:
    return client.post(f"/push/{action}/", data)


def test_the_browser_subscribes_and_unsubscribes_from_the_profile() -> None:
    user, browser = UserFactory(), Browser()
    client = signed_in(user)

    subscribed = post(client, "subscribe", browser.form())
    assert subscribed.json() == {"subscribed": True, "count": 1}
    assert PushSubscription.objects.get().user == user

    left = post(client, "unsubscribe", {"endpoint": browser.endpoint})
    assert left.json() == {"subscribed": False, "count": 0}


def test_the_device_is_named_after_the_browser() -> None:
    post(signed_in(UserFactory()), "subscribe", Browser().form())

    assert "Firefox" in PushSubscription.objects.get().device


def test_a_bad_subscription_is_refused() -> None:
    form = Browser("http://push.example.com/1").form()

    answer = post(signed_in(UserFactory()), "subscribe", form)

    assert answer.status_code == 400
    assert not PushSubscription.objects.exists()


def test_the_state_tells_whose_the_browser_is() -> None:
    owner, browser = UserFactory(), Browser()
    post(signed_in(owner), "subscribe", browser.form())

    other = post(signed_in(UserFactory()), "state", {"endpoint": browser.endpoint})

    assert other.json() == {"subscribed": False, "count": 0}


@pytest.mark.usefixtures("commit")
def test_a_test_push_reaches_the_browser(http: FakeHttp) -> None:
    user, browser = UserFactory(), Browser()
    client = signed_in(user)
    post(client, "subscribe", browser.form())

    answer = post(client, "test", {"endpoint": browser.endpoint})

    assert answer.status_code == 200
    assert "Firefox" in browser.read(http.to(browser.endpoint)[0].body)["body"]


def test_a_test_push_needs_this_browser_subscribed() -> None:
    answer = post(signed_in(UserFactory()), "test", {"endpoint": Browser().endpoint})

    assert answer.status_code == 400


def test_signing_out_unsubscribes_this_browser() -> None:
    user = UserFactory()
    here, elsewhere = (
        Browser("https://push.example.com/here"),
        Browser("https://push.example.com/away"),
    )
    client = signed_in(user)
    post(client, "subscribe", here.form())
    post(signed_in(user), "subscribe", elsewhere.form())

    client.post("/accounts/logout/")

    assert list(PushSubscription.objects.values_list("endpoint", flat=True)) == [elsewhere.endpoint]


@pytest.mark.parametrize("action", ["subscribe", "unsubscribe", "state", "test"])
def test_a_guest_is_sent_to_sign_in(action: str) -> None:
    answer = post(Client(), action, Browser().form())

    assert answer.status_code == 302
    assert "/accounts/login/" in answer["Location"]


def test_the_profile_offers_notifications_in_the_browser() -> None:
    page = signed_in(UserFactory()).get("/accounts/profile/").content.decode()

    assert "data-push" in page
    assert PanelKeys.public() in page


OLD = "https://push.example.com/old"
NEW = "https://push.example.com/new"


def resubscribe(client: Client, old: str, new: Browser, site: str = "same-origin") -> Any:
    form = {"old_endpoint": old, **new.form()}
    return client.post("/push/resubscribe/", form, HTTP_SEC_FETCH_SITE=site)


def test_the_worker_replaces_a_changed_subscription_without_a_csrf_token() -> None:
    user, new = UserFactory(), Browser(NEW)
    client = Client(enforce_csrf_checks=True, HTTP_USER_AGENT=FIREFOX)
    client.force_login(user)
    BrowserSubscriptions(user).add(OLD, new.p256dh, new.auth)

    answer = resubscribe(client, OLD, new)

    assert answer.json() == {"subscribed": True, "count": 1}
    saved = PushSubscription.objects.get()
    assert (saved.user, saved.endpoint, saved.p256dh) == (user, NEW, new.p256dh)


def test_signing_out_unsubscribes_the_replaced_subscription() -> None:
    client = signed_in(UserFactory())
    post(client, "subscribe", Browser(OLD).form())
    resubscribe(client, OLD, Browser(NEW))

    client.post("/accounts/logout/")

    assert not PushSubscription.objects.exists()


def test_another_users_subscription_is_not_replaced() -> None:
    owner = UserFactory()
    post(signed_in(owner), "subscribe", Browser(OLD).form())

    answer = resubscribe(signed_in(UserFactory()), OLD, Browser(NEW))

    assert answer.status_code == 400
    saved = PushSubscription.objects.get()
    assert (saved.user, saved.endpoint) == (owner, OLD)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", ""])
def test_a_resubscription_not_sent_by_the_panel_is_refused(site: str) -> None:
    client = signed_in(UserFactory())
    post(client, "subscribe", Browser(OLD).form())

    answer = resubscribe(client, OLD, Browser(NEW), site)

    assert answer.status_code == 403
    assert PushSubscription.objects.get().endpoint == OLD


def test_a_bad_new_subscription_keeps_the_old_one() -> None:
    client = signed_in(UserFactory())
    post(client, "subscribe", Browser(OLD).form())

    answer = resubscribe(client, OLD, Browser("http://push.example.com/new"))

    assert answer.status_code == 400
    assert PushSubscription.objects.get().endpoint == OLD


def test_a_guest_cannot_resubscribe() -> None:
    answer = resubscribe(Client(), OLD, Browser(NEW))

    assert answer.status_code == 302
    assert not PushSubscription.objects.exists()
