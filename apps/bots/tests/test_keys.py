from dataclasses import replace

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.bots.tests.conftest import SITE
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.core.models import BotKeyDelivery, KeyRevealPolicy, SiteSettings
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.subscription import MySubscription
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.keys.tests.test_views import issued_key
from apps.operations.models import Operation
from apps.portal.services.keys import MyKeys
from apps.portal.tests.test_portal import OnCommit, reveal_policy

pytestmark = pytest.mark.django_db


def bot_key_delivery(delivery: BotKeyDelivery) -> None:
    site = SiteSettings.load()
    site.bot_key_delivery = delivery
    site.save()


def test_no_keys_offers_a_new_one(bot: FakeAdapter, user: User) -> None:
    reply = bot.say("/keys")

    assert "no keys yet" in reply.text
    assert reply.buttons[0].action == "newkey"


def test_keys_lists_only_the_users_own(bot: FakeAdapter, user: User, awg_server: object) -> None:
    mine = issued_key(user=user, name="phone")
    issued_key(user=UserFactory(), name="stranger", public_key="other")

    reply = bot.say("/keys")

    assert reply.text == "• phone · Amsterdam · active"
    assert reply.buttons[0].action == f"card {mine.pk}"


def test_a_card_offers_the_actions(bot: FakeAdapter, user: User) -> None:
    key = issued_key(user=user)

    actions = [button.action for button in bot.press(f"card {key.pk}").buttons]

    assert actions == [f"show {key.pk}", f"reissue {key.pk}", f"delete {key.pk}", "keys", "menu"]


@pytest.mark.parametrize("action", ["card", "show", "reissue", "delete"])
def test_someone_elses_key_is_not_found(bot: FakeAdapter, user: User, action: str) -> None:
    key = issued_key(user=UserFactory())

    assert bot.press(f"{action} {key.pk} yes").text == "There is no such key."
    assert AccessKey.objects.get(pk=key.pk).status == KeyStatus.ACTIVE


def test_a_new_key_in_steps_then_shown_in_the_chat(
    bot: FakeAdapter,
    user: User,
    awg_server: FakeAwgServer,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    devices = bot.say("/newkey")  # the plan's only server is chosen by itself
    assert [b.action for b in devices.buttons][:2] == [
        f"newkey {SOURCE} windows",
        f"newkey {SOURCE} macos",
    ]

    with django_capture_on_commit_callbacks(execute=True):
        issuing = bot.press_button(devices, "Android")

    key = AccessKey.objects.get(user=user)
    assert (key.name, key.status) == ("Android", KeyStatus.ACTIVE)
    shown = bot.press_button(issuing, "Show the key")
    assert shown.private and shown.code.startswith("vpn://")
    assert shown.pictures and all(p.content.startswith(b"\x89PNG") for p in shown.pictures)
    (conf,) = shown.files
    assert conf.name == "Android.conf" and b"[Interface]" in conf.content
    assert shown.buttons[0].url == f"{SITE}/guides/android/"


def test_choose_a_server_when_the_plan_has_several(
    bot: FakeAdapter, user: User, awg_server: FakeAwgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    (amsterdam,) = MyKeys(user).instances()
    two = [amsterdam, replace(amsterdam, source="awg:2", title="Riga")]
    monkeypatch.setattr(MyKeys, "instances", lambda self: two)

    reply = bot.say("/newkey")

    assert [b.action for b in reply.buttons] == [f"newkey {SOURCE}", "newkey awg:2", "menu"]


def test_a_key_being_issued_asks_to_come_back(bot: FakeAdapter, user: User) -> None:
    key = issued_key(user=user, status=KeyStatus.ISSUING, profile="")

    reply = bot.press(f"show {key.pk} ios")

    assert not reply.code
    assert reply.buttons[0].action == f"show {key.pk} ios"


def test_link_only_sends_a_link_and_leaves_the_key_unshown(
    bot: FakeAdapter, user: User, awg_server: FakeAwgServer
) -> None:
    bot_key_delivery(BotKeyDelivery.LINK_ONLY)
    reveal_policy(KeyRevealPolicy.ONCE)
    key = issued_key(user=user)

    reply = bot.press(f"show {key.pk}")

    assert not reply.code and not reply.files and not reply.pictures
    assert reply.buttons[0].url == f"{SITE}/portal/keys/{key.pk}/"
    assert AccessKey.objects.get(pk=key.pk).first_viewed_at is None


def test_the_once_policy_holds_in_the_chat(
    bot: FakeAdapter, user: User, awg_server: FakeAwgServer
) -> None:
    reveal_policy(KeyRevealPolicy.ONCE)
    key = issued_key(user=user)

    first = bot.press(f"show {key.pk}")
    second = bot.press(f"show {key.pk}")

    assert first.code.startswith("vpn://")
    assert not second.code and second.buttons[0].action == f"reissue {key.pk}"


def test_reissue_asks_first_then_replaces_the_key(
    bot: FakeAdapter, user: User, awg_server: FakeAwgServer
) -> None:
    key = issued_key(user=user)

    question = bot.press(f"reissue {key.pk}")
    assert not Operation.objects.exists()

    done = bot.press_button(question, "Re-issue")
    again = bot.press_button(question, "Re-issue")  # a second press while the old key is revoked

    new = AccessKey.objects.exclude(pk=key.pk).get(user=user)
    assert done.buttons[0].action == f"show {new.pk}"
    assert again.text == "There is no such key."
    assert AccessKey.objects.filter(user=user).count() == 2


def test_delete_asks_first(bot: FakeAdapter, user: User, awg_server: FakeAwgServer) -> None:
    key = issued_key(user=user)

    question = bot.press(f"delete {key.pk}")
    assert not Operation.objects.exists()

    bot.press_button(question, "Delete")

    assert Operation.objects.filter(kind="keys.revoke").exists()
    assert bot.press_button(question, "Delete").text == "There is no such key."


class TestSubscription:
    def test_offers_to_create_the_link(self, bot: FakeAdapter, user: User) -> None:
        reply = bot.say("/sub")

        assert "no subscription link" in reply.text
        assert reply.buttons[0].action == "sub new"

    def test_creates_and_shows_the_link(self, bot: FakeAdapter, user: User) -> None:
        reply = bot.press("sub new")

        token = MySubscription(user).token()
        assert f"{SITE}/sub/{token}" in reply.text
        assert f"{SITE}/sub/{token}" in bot.say("/sub").text

    def test_a_new_link_asks_first(self, bot: FakeAdapter, user: User) -> None:
        old = MySubscription(user).renew()

        assert bot.press("sub new").buttons[0].action == "sub new yes"
        assert MySubscription(user).token() == old

        bot.press("sub new yes")
        assert MySubscription(user).token() != old

    def test_off_when_keys_are_shown_once(self, bot: FakeAdapter, user: User) -> None:
        reveal_policy(KeyRevealPolicy.ONCE)

        assert "turned off" in bot.press("sub new").text
        assert MySubscription(user).token() is None
