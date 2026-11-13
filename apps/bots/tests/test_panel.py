"""The bot as one panel: buttons redraw it in place, anything else opens it at the bottom."""

import pytest

from apps.accounts.models import User
from apps.bots.services.replies import Placement
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.keys.tests.fake_server import FakeAwgServer
from apps.keys.tests.test_views import issued_key
from apps.portal.tests.test_portal import OnCommit

pytestmark = pytest.mark.django_db


def test_going_through_the_sections_changes_one_message(bot: FakeAdapter, user: User) -> None:
    menu = bot.say("/menu")
    keys = bot.press_button(menu, "My keys")
    menu_again = bot.press_button(keys, "Menu")
    support = bot.press_button(menu_again, "Support")
    bot.press_button(support, "My requests")

    (panel,) = bot.panels
    assert panel.edits == 4
    assert panel.reply is not None and panel.reply.title == "My requests"
    assert bot.kinds == ["user", "panel"]


def test_typed_text_opens_the_panel_anew_at_the_bottom(bot: FakeAdapter, user: User) -> None:
    bot.press_button(bot.say("/menu"), "Support")

    bot.say("Hello, nothing works")

    assert bot.kinds == ["user", "panel", "user", "panel"]
    assert bot.replies[-1].placement == Placement.NEW


def test_a_key_comes_in_messages_of_its_own_with_the_panel_below(
    bot: FakeAdapter,
    user: User,
    awg_server: FakeAwgServer,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    devices = bot.say("/newkey")
    with django_capture_on_commit_callbacks(execute=True):
        issuing = bot.press_button(devices, "Android")

    shown = bot.press_button(issuing, "Show the key")

    assert shown.placement == Placement.NEW
    assert bot.kinds[:2] == ["user", "panel"]
    assert bot.kinds[2] == "code" and bot.kinds[-2:] == ["file", "panel"]
    assert set(bot.kinds[3:-2]) == {"picture"}
    assert bot.panels[0].edits == 1  # the device choice became «issuing», then stayed above
    assert shown.clear_pressed  # the panel above the key loses its buttons
    assert bot.panels[0].reply is not None and bot.panels[0].reply.buttons == ()
    assert [b.label for b in shown.buttons][-2:] == ["Back", "Menu"]


def test_after_the_key_the_buttons_redraw_the_new_panel(
    bot: FakeAdapter, user: User, awg_server: FakeAwgServer
) -> None:
    key = issued_key(user=user)
    shown = bot.press(f"show {key.pk}")

    card = bot.press_button(shown, "Back")

    assert card.placement == Placement.REPLACE
    assert bot.panels[-1].reply == card and bot.panels[-1].edits == 1


def test_a_notification_opens_the_next_panel_below_it(bot: FakeAdapter, user: User) -> None:
    menu = bot.say("/menu")
    bot.notify("Your key is ready")

    status = bot.press_button(menu, "Status")
    keys = bot.press_button(status, "My keys")

    assert status.placement == Placement.NEW
    assert keys.placement == Placement.REPLACE  # the mark is spent by the first panel
    assert bot.kinds == ["user", "panel", "notification", "panel"]


def test_a_notification_marks_only_its_own_chat(user: User) -> None:
    anna, other = FakeAdapter("1001"), FakeAdapter("2002")
    other.notify("Not for Anna")

    anna.say("/menu")

    assert anna.press("status").placement == Placement.REPLACE


def test_sections_have_a_title_back_and_menu(bot: FakeAdapter, user: User) -> None:
    key = issued_key(user=user)

    card = bot.press(f"card {key.pk}")

    assert card.title == key.name
    assert [b.label for b in card.buttons][-2:] == ["Back", "Menu"]
    assert card.buttons[-2].action == "keys"


def test_a_redrawn_panel_keeps_its_buttons(bot: FakeAdapter, user: User) -> None:
    keys = bot.press_button(bot.say("/menu"), "My keys")

    assert not keys.clear_pressed
    assert bot.panels[-1].reply is not None and bot.panels[-1].reply.buttons


def test_a_typed_command_clears_no_panel(bot: FakeAdapter, user: User) -> None:
    bot.say("/menu")

    assert not bot.say("/keys").clear_pressed
