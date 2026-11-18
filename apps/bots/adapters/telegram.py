"""The Telegram bot (aiogram 3, DESIGN.md 6.8).

It receives updates by long polling, or by a webhook when LAMNEZIA_TELEGRAM_WEBHOOK is on:
Telegram posts them to SITE_URL/bots/telegram/, and Caddy or nginx passes them to this process.
It answers only in private chats, so a reply with a key never shows up in a group. A photo the
user sends, or an image sent as a file, goes to the core with its caption as the text. The bot
calls the Bot API server and the proxy of the settings (apps/core/services/outbound.py).
"""

import asyncio
import hashlib
import hmac
from collections.abc import Iterable
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from aiogram.types import User as TelegramUser
from aiogram.utils.formatting import Bold, Code, Text
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import translation

from apps.accounts.models import Messenger
from apps.bots.adapters.base import BotAdapter, adapters
from apps.bots.services.menu import MENU, STAFF_MENU, MenuCommand, staff_chats
from apps.bots.services.replies import (
    Attachment,
    Button,
    Incoming,
    Placement,
    Reply,
    split_text,
)
from apps.core.models import SiteSettings
from apps.core.services.outbound import TelegramApi
from apps.support.services.images import MAX_IMAGE_SIZE

# An image sent as a file rather than as a photo, to keep it uncompressed.
_IMAGE_FILE = F.document.mime_type.startswith("image/")

# Telegram limits a message text to 4096 characters, counted in UTF-16 code units.
TEXT_LIMIT = 4096


def api_session(api: TelegramApi) -> AiohttpSession:
    """The bot's HTTP session: to the Bot API server of the settings, through their proxy."""
    proxy = api.proxy.url if api.proxy else None
    return AiohttpSession(proxy=proxy, api=TelegramAPIServer.from_base(api.server))


@adapters.register
class TelegramAdapter(BotAdapter):
    messenger = Messenger.TELEGRAM

    @classmethod
    def token(cls) -> str:
        # Read anew each time: without a shared cache the panel's change would not reach here.
        token: str = SiteSettings.load().telegram_bot_token
        return token

    async def run(self, token: str) -> None:
        bot = Bot(token, session=api_session(TelegramApi.from_settings()))
        try:
            me = await bot.get_me()  # fails at once on a wrong token
            await set_menu(bot)
            dispatcher = self.dispatcher()
            self.identity = f"@{me.username}"
            if settings.TELEGRAM_WEBHOOK:
                await TelegramWebhook(token).serve(bot, dispatcher)
            else:
                await bot.delete_webhook()
                await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
        finally:
            await bot.session.close()

    def dispatcher(self) -> Dispatcher:
        router = Router()
        router.message.register(
            self.on_message, F.chat.type == ChatType.PRIVATE, F.text | F.photo | _IMAGE_FILE
        )
        router.callback_query.register(
            self.on_button, F.message.chat.type == ChatType.PRIVATE, F.data
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        return dispatcher

    async def on_message(self, message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        incoming = self._incoming(
            message.from_user,
            text=message.text or message.caption or "",
            replied=_replied(message),
            picture=await _picture(message, bot),
        )
        reply = await self.answer(incoming)
        await TelegramChat(bot, message.chat.id).show(reply)

    async def on_button(self, callback: CallbackQuery, bot: Bot) -> None:
        reply = await self.answer(self._incoming(callback.from_user, action=callback.data or ""))
        if reply.placement == Placement.POPUP:
            await callback.answer(reply.text)
            return
        await callback.answer()  # stops the spinner on the pressed button
        # A message older than 48 hours comes inaccessible: its panel cannot be redrawn.
        pressed = callback.message if isinstance(callback.message, Message) else None
        await TelegramChat(bot, callback.from_user.id).show(reply, pressed)

    def _incoming(
        self,
        sender: TelegramUser,
        *,
        text: str = "",
        action: str = "",
        replied: str = "",
        picture: Attachment | None = None,
    ) -> Incoming:
        return Incoming(
            self.messenger,
            str(sender.id),
            sender.username or "",
            text=text,
            action=action,
            locale=sender.language_code or "",
            replied=replied,
            picture=picture,
        )


async def _picture(message: Message, bot: Bot) -> Attachment | None:
    """The photo of the message in its largest size (Telegram sends photos as JPEG), or an
    image sent as a file. One bigger than the support takes is not downloaded.
    """
    file: PhotoSize | Document
    if message.photo:
        file = message.photo[-1]
        name, kind = f"{file.file_unique_id}.jpg", "image/jpeg"
    elif message.document is not None and _is_image(message.document):
        file = message.document
        name, kind = file.file_name or file.file_unique_id, file.mime_type or ""
    else:
        return None
    if file.file_size is not None and file.file_size > MAX_IMAGE_SIZE:
        return Attachment.not_downloaded(name, kind, file.file_size)
    content = await bot.download(file)
    if content is None:
        return None
    return Attachment(name, content.read(), kind)


def _is_image(document: Document) -> bool:
    return (document.mime_type or "").startswith("image/")


def _replied(message: Message) -> str:
    """The text of the bot's message this one replies to; empty for anything else."""
    original = message.reply_to_message
    if original is None or original.from_user is None or not original.from_user.is_bot:
        return ""
    return original.text or original.caption or ""


class TelegramChat:
    """Shows a core reply in one private chat.

    The panel (the title in bold, the text and the buttons) is redrawn in place on the
    message whose button was pressed. Otherwise the `vpn://` link goes as monospace, the
    pictures as photos and the files as documents, each a message of its own, and a new panel
    follows below them; the pressed panel loses its buttons. When the old panel cannot be
    edited, or the text is longer than one message, a new one is sent instead.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def show(self, reply: Reply, pressed: Message | None = None) -> None:
        if (
            reply.placement == Placement.REPLACE
            and pressed is not None
            and await self._redraw(pressed.message_id, reply)
        ):
            return
        if reply.clear_pressed and pressed is not None:
            await self._clear(pressed.message_id)
        if reply.code:
            await self._bot.send_message(self._chat_id, **Text(Code(reply.code)).as_kwargs())
        for picture in reply.pictures:
            await self._bot.send_photo(self._chat_id, _file(picture))
        for file in reply.files:
            await self._bot.send_document(self._chat_id, _file(file))
        *parts, last = panel_texts(reply)
        for part in parts:
            await self._bot.send_message(self._chat_id, **part.as_kwargs())
        await self._bot.send_message(
            self._chat_id, **last.as_kwargs(), reply_markup=keyboard_of(reply.buttons)
        )

    async def _clear(self, message_id: int) -> None:
        """Take the buttons off an old panel; a deleted or too old one keeps them."""
        with suppress(TelegramBadRequest):
            await self._bot.edit_message_reply_markup(
                chat_id=self._chat_id, message_id=message_id, reply_markup=None
            )

    async def _redraw(self, message_id: int, reply: Reply) -> bool:
        """Whether the panel now shows the reply."""
        texts = panel_texts(reply)
        if len(texts) > 1:  # a panel longer than one message is posted anew
            return False
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                reply_markup=keyboard_of(reply.buttons),
                **texts[0].as_kwargs(),
            )
        except TelegramBadRequest as error:
            # Pressing «Menu» on the menu changes nothing; any other refusal (the message is
            # deleted, it is a photo of an older reply) means a new panel.
            return "message is not modified" in error.message
        return True


def panel_texts(reply: Reply) -> list[Text]:
    """The panel as messages: the title in bold with the text, which goes on in further
    messages when it is over the limit. The buttons go with the last one.
    """
    head = f"{reply.title}\n\n" if reply.title else ""
    first, *rest = split_text(reply.text, TEXT_LIMIT - _utf16(head), _utf16)
    return [Text(Bold(reply.title), "\n\n", first) if head else Text(first)] + [
        Text(part) for part in rest
    ]


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _file(attachment: Attachment) -> BufferedInputFile:
    return BufferedInputFile(attachment.content, attachment.name)


def keyboard_of(buttons: tuple[Button, ...]) -> InlineKeyboardMarkup | None:
    """One button a row: labels such as server names are too long to share one."""
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button.label, url=button.url)
                if button.url
                else InlineKeyboardButton(text=button.label, callback_data=button.action)
            ]
            for button in buttons
        ]
    )


async def set_menu(bot: Bot) -> None:
    """The command menu in every panel language; users of other languages get the default one.

    A staff member's chat gets the staff commands too, in their language; a chat Telegram refuses
    (the bot is blocked) is passed over.
    """
    languages = [
        ("", settings.LANGUAGE_CODE),
        *((code, code) for code, _name in settings.LANGUAGES),
    ]
    for language_code, language in languages:
        await bot.set_my_commands(_commands(MENU, language), language_code=language_code)
    for chat in await sync_to_async(staff_chats)(Messenger.TELEGRAM):
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.set_my_commands(
                _commands((*MENU, *STAFF_MENU), chat.language),
                scope=BotCommandScopeChat(chat_id=chat.external_id),
            )


def _commands(menu: Iterable[MenuCommand], language: str) -> list[BotCommand]:
    with translation.override(language):
        return [BotCommand(command=c.name, description=str(c.description)) for c in menu]


class TelegramWebhook:
    """The HTTP server Telegram posts updates to, at SITE_URL + PATH.

    Telegram sends the secret in a header with every update; requests without it are refused.
    The secret comes from the bot token and SECRET_KEY, so it survives restarts.
    """

    PATH = "/bots/telegram/"

    def __init__(self, token: str) -> None:
        self.secret = hmac.new(
            settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256
        ).hexdigest()

    @property
    def url(self) -> str:
        site: str = settings.SITE_URL
        return site.rstrip("/") + self.PATH

    def application(self, bot: Bot, dispatcher: Dispatcher) -> web.Application:
        app = web.Application()
        SimpleRequestHandler(dispatcher, bot, secret_token=self.secret).register(app, self.PATH)
        setup_application(app, dispatcher, bot=bot)
        return app

    async def serve(self, bot: Bot, dispatcher: Dispatcher) -> None:
        runner = web.AppRunner(self.application(bot, dispatcher))
        await runner.setup()
        await web.TCPSite(runner, port=settings.BOTS_WEBHOOK_PORT).start()
        await bot.set_webhook(
            self.url,
            secret_token=self.secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        try:
            await asyncio.Event().wait()  # until the process stops
        finally:
            await runner.cleanup()
