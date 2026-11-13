"""The Telegram adapter: updates in, Bot API requests out, kept by a recording session."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendDocument,
    SendMessage,
    SendPhoto,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, File, Message, PhotoSize, Update, User
from aiohttp.test_utils import TestClient, TestServer
from django.utils import translation
from django.utils.translation import gettext

from apps.accounts.models import Messenger
from apps.bots.adapters.telegram import TEXT_LIMIT, TelegramAdapter, TelegramWebhook, set_menu
from apps.bots.services.core import BotCore
from apps.bots.services.replies import Attachment, Button, Incoming, Placement, Reply
from apps.bots.services.session import SiteLinks
from apps.bots.tests.conftest import SITE, link
from apps.keys.models import AccessKey
from apps.keys.tests.conftest import SOURCE
from apps.portal.tests.test_portal import subscriber

ANNA = User(id=501, is_bot=False, first_name="Anna", username="anna", language_code="ru")


PHOTO = b"\xff\xd8\xff photo"


class RecordingSession(BaseSession):
    """Answers every Bot API call at once and keeps the calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.refuse_edits = ""  # the error Telegram gives to an edit of a message

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.calls.append(method)
        if self.refuse_edits and isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, self.refuse_edits)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u1", file_path="photos/1.jpg")
        if method.__returning__ is Message:
            return Message(message_id=1, date=_now(), chat=Chat(id=ANNA.id, type="private"))
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        yield PHOTO

    def sent(self, kind: type[TelegramMethod[Any]]) -> list[Any]:
        return [call for call in self.calls if isinstance(call, kind)]


class StubCore(BotCore):
    """Gives one reply to anything and keeps what the adapter passed."""

    def __init__(self, reply: Reply) -> None:
        super().__init__()
        self.reply = reply
        self.received: list[Incoming] = []

    def handle(self, incoming: Incoming) -> Reply:
        self.received.append(incoming)
        return self.reply


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot("123:secret", session=session)


def _now() -> datetime:
    return datetime.now(UTC)


def _message(text: str, chat_type: str = "private") -> Update:
    chat = Chat(id=ANNA.id if chat_type == "private" else -100, type=chat_type)
    return Update(
        update_id=1,
        message=Message(message_id=7, date=_now(), chat=chat, from_user=ANNA, text=text),
    )


def _press(action: str) -> Update:
    message = Message(message_id=7, date=_now(), chat=Chat(id=ANNA.id, type="private"))
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="q1", from_user=ANNA, chat_instance="c", message=message, data=action
        ),
    )


async def _feed(reply: Reply, bot: Bot, update: Update) -> StubCore:
    core = StubCore(reply)
    await TelegramAdapter(core).dispatcher().feed_update(bot, update)
    return core


@pytest.mark.asyncio
async def test_a_message_goes_to_the_core_with_the_sender(
    bot: Bot, session: RecordingSession
) -> None:
    core = await _feed(Reply("Linked."), bot, _message("/start abc"))

    assert core.received == [
        Incoming(Messenger.TELEGRAM, "501", "anna", text="/start abc", locale="ru")
    ]
    (sent,) = session.sent(SendMessage)
    assert (sent.chat_id, sent.text, sent.reply_markup) == (501, "Linked.", None)


@pytest.mark.asyncio
async def test_a_reply_to_a_message_of_the_bot_brings_its_text(bot: Bot) -> None:
    panel_bot = User(id=9, is_bot=True, first_name="Lamnezia")
    chat = Chat(id=ANNA.id, type="private")
    original = Message(
        message_id=3, date=_now(), chat=chat, from_user=panel_bot, text="New request #12 from boris"
    )
    message = Message(
        message_id=7,
        date=_now(),
        chat=chat,
        from_user=ANNA,
        text="On it",
        reply_to_message=original,
    )

    core = await _feed(Reply("Sent."), bot, Update(update_id=1, message=message))

    assert core.received[0].replied == "New request #12 from boris"


@pytest.mark.asyncio
async def test_buttons_are_inline_and_a_press_runs_their_action(
    bot: Bot, session: RecordingSession
) -> None:
    reply = Reply("Menu", (Button("My keys", "keys"), Button("FAQ", url="https://vpn.example/faq")))

    core = await _feed(reply, bot, _press("card 12"))

    assert core.received[0].action == "card 12"
    assert len(session.sent(AnswerCallbackQuery)) == 1
    (sent,) = session.sent(SendMessage)
    rows = sent.reply_markup.inline_keyboard
    assert [(b.text, b.callback_data, b.url) for (b,) in rows] == [
        ("My keys", "keys", None),
        ("FAQ", None, "https://vpn.example/faq"),
    ]


@pytest.mark.asyncio
async def test_a_key_comes_as_code_a_photo_and_a_document(
    bot: Bot, session: RecordingSession
) -> None:
    qr = Attachment("key.png", b"png", "image/png")
    conf = Attachment("key.conf", b"[Interface]", "text/plain")
    reply = Reply(
        "Your key", (Button("My keys", "keys"),), code="vpn://abc", pictures=(qr,), files=(conf,)
    )

    await _feed(reply, bot, _message("/show 12"))

    calls: list[Any] = session.calls
    assert [type(call) for call in calls] == [SendMessage, SendPhoto, SendDocument, SendMessage]
    code, photo, document, panel = calls
    assert code.text == "vpn://abc"
    assert [entity.type for entity in code.entities] == ["code"]
    assert code.reply_markup is None
    assert (photo.photo.filename, photo.reply_markup) == ("key.png", None)
    assert (document.document.filename, document.reply_markup) == ("key.conf", None)
    assert panel.text == "Your key"
    assert panel.reply_markup.inline_keyboard[0][0].callback_data == "keys"


@pytest.mark.asyncio
async def test_a_key_from_a_pressed_button_still_opens_a_new_panel(
    bot: Bot, session: RecordingSession
) -> None:
    reply = Reply("Your key", code="vpn://abc", placement=Placement.NEW)

    await _feed(reply, bot, _press("show 12"))

    assert session.sent(EditMessageText) == []
    assert [call.text for call in session.sent(SendMessage)] == ["vpn://abc", "Your key"]


@pytest.mark.asyncio
async def test_a_press_redraws_the_panel_in_place(bot: Bot, session: RecordingSession) -> None:
    reply = Reply("phone · active", (Button("Back", "keys"),), title="phone")

    await _feed(reply.placed(Placement.REPLACE), bot, _press("card 12"))

    assert session.sent(SendMessage) == []
    (edit,) = session.sent(EditMessageText)
    assert (edit.chat_id, edit.message_id) == (ANNA.id, 7)
    assert edit.text == "phone\n\nphone · active"
    assert [entity.type for entity in edit.entities] == ["bold"]
    assert edit.reply_markup.inline_keyboard[0][0].callback_data == "keys"


@pytest.mark.asyncio
async def test_a_panel_that_cannot_be_edited_is_sent_anew(
    bot: Bot, session: RecordingSession
) -> None:
    session.refuse_edits = "Bad Request: message to edit not found"

    await _feed(Reply("Menu", placement=Placement.REPLACE), bot, _press("menu"))

    (sent,) = session.sent(SendMessage)
    assert sent.text == "Menu"


@pytest.mark.asyncio
async def test_a_panel_that_did_not_change_is_left_as_it_is(
    bot: Bot, session: RecordingSession
) -> None:
    session.refuse_edits = "Bad Request: message is not modified: the content is the same"

    await _feed(Reply("Menu", placement=Placement.REPLACE), bot, _press("menu"))

    assert session.sent(SendMessage) == []


@pytest.mark.asyncio
async def test_group_chats_are_ignored(bot: Bot, session: RecordingSession) -> None:
    core = await _feed(Reply("Your key", private=True), bot, _message("/keys", "supergroup"))

    assert core.received == []
    assert session.calls == []


@pytest.mark.asyncio
async def test_the_menu_is_set_in_every_language(bot: Bot, session: RecordingSession) -> None:
    await set_menu(bot)

    menus = {call.language_code: call.commands for call in session.sent(SetMyCommands)}
    assert set(menus) == {"", "en", "ru"}
    assert [c.command for c in menus["en"]][:3] == ["menu", "status", "keys"]
    with translation.override("ru"):
        assert menus["ru"][2].description == gettext("My keys")


def test_the_webhook_lives_on_the_panel_address(settings: Any) -> None:
    settings.SITE_URL = "https://vpn.example/"

    webhook = TelegramWebhook("123:secret")

    assert webhook.url == "https://vpn.example/bots/telegram/"
    assert webhook.secret == TelegramWebhook("123:secret").secret != TelegramWebhook("1:x").secret


@pytest.mark.asyncio
async def test_the_webhook_takes_updates_with_the_secret_only(
    bot: Bot, session: RecordingSession
) -> None:
    core = StubCore(Reply("Menu"))
    webhook = TelegramWebhook("123:secret")
    app = webhook.application(bot, TelegramAdapter(core).dispatcher())
    update = _message("/menu").model_dump(mode="json", exclude_none=True)

    async with TestClient(TestServer(app)) as client:
        refused = await client.post(webhook.PATH, json=update)
        taken = await client.post(
            webhook.PATH,
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": webhook.secret},
        )
        # The update is handled in the background, after Telegram already got its answer.
        async with asyncio.timeout(5):
            while not session.sent(SendMessage):
                await asyncio.sleep(0.01)

    assert (refused.status, taken.status) == (401, 200)
    assert [incoming.text for incoming in core.received] == ["/menu"]


@pytest.fixture
def anna() -> None:
    """Anna's Telegram linked to a user with a plan, committed for the core's own thread."""
    link(subscriber(max_keys=2), str(ANNA.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_new_key_from_the_menu_to_the_files(
    bot: Bot, session: RecordingSession, awg_server: object, anna: None
) -> None:
    dispatcher = TelegramAdapter(BotCore(links=SiteLinks(SITE))).dispatcher()

    await dispatcher.feed_update(bot, _message("/newkey"))
    await dispatcher.feed_update(bot, _press(f"newkey {SOURCE} android"))
    key = await AccessKey.objects.aget()
    await dispatcher.feed_update(bot, _press(f"show {key.pk}"))

    (redrawn,) = session.sent(EditMessageText)  # the device choice became «issuing»
    assert redrawn.message_id == 7
    *_, code, panel = session.sent(SendMessage)
    assert code.text.startswith("vpn://")
    assert all(photo.photo.data.startswith(b"\x89PNG") for photo in session.sent(SendPhoto))
    (conf,) = session.sent(SendDocument)
    assert conf.document.filename == "Android.conf"
    assert isinstance(session.calls[-1], SendMessage) and "Android" in panel.text


def _photo(caption: str = "") -> Update:
    sizes = [
        PhotoSize(file_id=f"f{side}", file_unique_id=f"p{side}", width=side, height=side)
        for side in (90, 1280)
    ]
    message = Message(
        message_id=7,
        date=_now(),
        chat=Chat(id=ANNA.id, type="private"),
        from_user=ANNA,
        photo=sizes,
        caption=caption or None,
    )
    return Update(update_id=3, message=message)


@pytest.mark.asyncio
async def test_a_photo_goes_to_the_core_with_its_caption(
    bot: Bot, session: RecordingSession
) -> None:
    core = await _feed(Reply("Sent."), bot, _photo("The error"))

    (incoming,) = core.received
    assert incoming.text == "The error"
    assert incoming.picture == Attachment("p1280.jpg", PHOTO, "image/jpeg")
    (asked,) = session.sent(GetFile)
    assert asked.file_id == "f1280"  # the largest size


@pytest.mark.asyncio
async def test_a_popup_answers_the_press_and_leaves_the_panel(
    bot: Bot, session: RecordingSession
) -> None:
    await _feed(Reply("Too many commands.", placement=Placement.POPUP), bot, _press("keys"))

    (answer,) = session.sent(AnswerCallbackQuery)
    assert answer.text == "Too many commands."
    assert session.sent(SendMessage) == [] and session.sent(EditMessageText) == []


@pytest.mark.asyncio
async def test_a_new_panel_below_takes_the_buttons_off_the_pressed_one(
    bot: Bot, session: RecordingSession
) -> None:
    reply = Reply("Your key", code="vpn://abc").placed(Placement.NEW, clear_pressed=True)

    await _feed(reply, bot, _press("show 12"))

    (cleared,) = session.sent(EditMessageReplyMarkup)
    assert (cleared.message_id, cleared.reply_markup) == (7, None)
    assert [call.text for call in session.sent(SendMessage)] == ["vpn://abc", "Your key"]


@pytest.mark.asyncio
async def test_a_panel_over_the_limit_goes_in_several_messages(
    bot: Bot, session: RecordingSession
) -> None:
    line = "🔑 " + "a" * 97 + "\n"  # the key counts twice in Telegram
    reply = Reply(line * 50, (Button("Menu", "menu"),), title="Keys", placement=Placement.REPLACE)

    await _feed(reply, bot, _press("keys"))

    assert session.sent(EditMessageText) == []
    sent = session.sent(SendMessage)
    assert len(sent) == 2
    assert all(len(call.text.encode("utf-16-le")) // 2 <= TEXT_LIMIT for call in sent)
    assert sent[0].text.startswith("Keys\n\n🔑")
    assert [call.reply_markup is not None for call in sent] == [False, True]
