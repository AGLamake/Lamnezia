"""A messenger adapter for tests: one chat whose user types commands and presses buttons.

It keeps the chat as a messenger shows it: the messages from top to bottom, where a reply's
key link, pictures and files are messages of their own and its panel is redrawn in place.
"""

from dataclasses import dataclass, replace

from apps.accounts.models import Messenger
from apps.bots.adapters.base import BotAdapter
from apps.bots.services.core import BotCore
from apps.bots.services.replies import Attachment, Incoming, Placement, Reply
from apps.bots.services.session import Panels


@dataclass
class ChatMessage:
    """One message in the chat. `kind` is panel, code, picture, file, notification or popup
    (a note over the pressed button).
    """

    kind: str
    text: str
    reply: Reply | None = None
    edits: int = 0


class FakeAdapter(BotAdapter):
    messenger = Messenger.TELEGRAM

    def __init__(self, external_id: str = "1001", core: BotCore | None = None) -> None:
        super().__init__(core)
        self.external_id = external_id
        self.replies: list[Reply] = []
        self.chat: list[ChatMessage] = []

    @classmethod
    def token(cls) -> str:
        return "fake-token"

    async def run(self, token: str) -> None:
        """Nothing to receive: the test sends each update itself."""

    def say(self, text: str, locale: str = "en") -> Reply:
        self.chat.append(ChatMessage("user", text))
        return self._send(
            Incoming(self.messenger, self.external_id, "anna", text=text, locale=locale)
        )

    def send_picture(self, picture: Attachment, caption: str = "") -> Reply:
        """A photo from the user, with an optional caption."""
        self.chat.append(ChatMessage("user", caption))
        return self._send(
            Incoming(self.messenger, self.external_id, "anna", text=caption, picture=picture)
        )

    def reply_to(self, notification: str, text: str) -> Reply:
        """Answer a message of the bot (a notification) with a messenger's «Reply»."""
        self.chat.append(ChatMessage("user", text))
        return self._send(
            Incoming(self.messenger, self.external_id, "anna", text=text, replied=notification)
        )

    def press(self, action: str, locale: str = "en") -> Reply:
        return self._send(
            Incoming(self.messenger, self.external_id, "anna", action=action, locale=locale)
        )

    def press_button(self, reply: Reply, label: str) -> Reply:
        """Press the button of `reply` with this label, as a user would."""
        (button,) = [button for button in reply.buttons if button.label == label]
        return self.press(button.action)

    def notify(self, text: str) -> None:
        """A notification from the panel, as the notifications worker delivers it."""
        self.chat.append(ChatMessage("notification", text))
        Panels().bury(self.messenger, self.external_id)

    @property
    def panels(self) -> list[ChatMessage]:
        return [message for message in self.chat if message.kind == "panel"]

    @property
    def kinds(self) -> list[str]:
        return [message.kind for message in self.chat]

    def _send(self, incoming: Incoming) -> Reply:
        reply = self.core.handle(incoming)
        self.replies.append(reply)
        self._show(reply)
        return reply

    def _show(self, reply: Reply) -> None:
        panel = ChatMessage("panel", reply.text, reply)
        if reply.placement == Placement.POPUP:
            self.chat.append(ChatMessage("popup", reply.text))
            return
        if reply.placement == Placement.REPLACE and self.panels:
            current = self.panels[-1]  # the panel whose button the test pressed
            current.text, current.reply, current.edits = reply.text, reply, current.edits + 1
            return
        pressed = self.panels[-1].reply if self.panels else None
        if reply.clear_pressed and pressed is not None:
            self.panels[-1].reply = replace(pressed, buttons=())
        if reply.code:
            self.chat.append(ChatMessage("code", reply.code))
        self.chat.extend(ChatMessage("picture", item.name) for item in reply.pictures)
        self.chat.extend(ChatMessage("file", item.name) for item in reply.files)
        self.chat.append(panel)
