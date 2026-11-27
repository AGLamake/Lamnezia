"""The Telegram command menu of one chat, set again when its user's role or link changes.

The bot sets the menus of the staff chats when it starts (`set_menu` in adapters/telegram.py).
A member who gets a staff role or links Telegram later, or loses the role or the link, gets the
right menu here through the Bot API, without a restart of the bot: a staff chat lists the staff
commands too, any other chat falls back to the bot's common menu.
"""

import json
from collections.abc import Iterable
from typing import Self

from django.conf import settings
from django.utils import translation

from apps.accounts.models import STAFF_ROLES, LinkedAccount, Messenger
from apps.bots.services.menu import MENU, STAFF_MENU, MenuCommand
from apps.core.models import SiteSettings
from apps.core.services.outbound import TelegramApi
from apps.notifications.services.channels import DeliveryError, HttpClient


class ChatMenu:
    """Sets the menu of a Telegram chat by its user's role; a refused chat is passed over."""

    def __init__(self, token: str, http: HttpClient, api: TelegramApi) -> None:
        self._token = token
        self._http = http
        self._api = api

    @classmethod
    def from_settings(cls) -> Self:
        token = SiteSettings.load().telegram_bot_token
        return cls(token, HttpClient(settings.NOTIFICATION_TIMEOUT), TelegramApi.from_settings())

    def refresh(self, chat_id: str) -> None:
        """Staff commands for a staff member's chat, the common menu for any other.

        A chat the bot cannot write to (blocked, never started) is left as it is; a failure
        worth another attempt raises `DeliveryError`.
        """
        if not self._token:
            return
        scope = {"type": "chat", "chat_id": chat_id}
        account = (
            LinkedAccount.objects.filter(messenger=Messenger.TELEGRAM, external_id=chat_id)
            .select_related("user")
            .first()
        )
        try:
            if account is not None and account.user.role in STAFF_ROLES:
                language = account.user.notification_language
                commands = _commands((*MENU, *STAFF_MENU), language)
                self._call("setMyCommands", {"commands": commands, "scope": scope})
            else:
                self._call("deleteMyCommands", {"scope": scope})
        except DeliveryError as error:
            if error.retryable:
                raise

    def _call(self, method: str, body: dict[str, object]) -> None:
        url = self._api.method_url(self._token, method)
        self._http.post(url, json.dumps(body).encode(), api=self._api)


def _commands(menu: Iterable[MenuCommand], language: str) -> list[dict[str, str]]:
    with translation.override(language):
        return [{"command": c.name, "description": str(c.description)} for c in menu]
