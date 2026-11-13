"""Writing to the staff (DESIGN.md 6.7, 6.8): a typed message that answers no question goes to the
support chat; requests are opened, read and answered in steps. A picture sent with the message
goes along as its image. Everything the user writes here is marked with the messenger, so the
staff's answers come back to it.
"""

from collections.abc import Mapping
from functools import cached_property

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.translation import gettext as _

from apps.bots.commands.base import CommandSet, Handler, bullets, nav, number, unavailable
from apps.bots.services.replies import Button, Command, Reply
from apps.support.models import Conversation, TicketType
from apps.support.services.chat import MyChat
from apps.support.services.images import Upload
from apps.support.services.thread import TooManyMessagesError
from apps.support.services.tickets import (
    ClosedConversationError,
    MyRequests,
    TooManyRequestsError,
)

# The command a typed message outside of any dialog becomes.
CHAT = "chat"
# Request types opened by a description; "a server does not work" has its own command.
_TYPES = (TicketType.BUG, TicketType.QUESTION, TicketType.OTHER)
_LISTED = 10
_SHOWN_MESSAGES = 5
_MESSAGE_LENGTH = 500


class SupportCommands(CommandSet):
    def handlers(self) -> Mapping[str, Handler]:
        return {
            "support": self.support,
            CHAT: self.chat_message,
            "ticket": self.new_request,
            "requests": self.my_requests,
            "request": self.request,
            "answer": self.answer,
        }

    @cached_property
    def requests(self) -> MyRequests:
        return MyRequests(self.user, self.chat.client, self.chat.messenger)

    def support(self, command: Command) -> Reply:
        return Reply(
            _(
                "Write your question in a message here: it goes to the administrators. "
                "Or open a request: it has a topic and a status."
            ),
            (
                Button(_("New request"), "ticket"),
                Button(_("My requests"), "requests"),
                *nav(),
            ),
            title=_("Support"),
        )

    def chat_message(self, command: Command) -> Reply:
        if not command.text and command.picture is None:
            return self.support(command)
        try:
            MyChat(self.user, self.chat.messenger).send(command.text, _image(command))
        except TooManyMessagesError:
            return Reply(_("Too many messages in a minute. Please wait a little."), nav())
        except ValidationError as error:
            return Reply(" ".join(error.messages), nav())
        return Reply(_("Sent to the administrators."), nav(), title=_("Support"))

    def new_request(self, command: Command) -> Reply:
        """`ticket`: choose the type; `ticket <type>`: wait for the description; with the typed
        description: open the request.
        """
        title = _("New request")
        if not command.args:
            buttons = tuple(Button(str(kind.label), f"ticket {kind.value}") for kind in _TYPES)
            return Reply(
                _("What is the request about?"),
                (*buttons, Button(str(TicketType.SERVER_DOWN.label), "down"), *nav("support")),
                title=title,
            )
        kind = next((kind for kind in _TYPES if kind.value == command.arg(0)), None)
        if kind is None:
            return unavailable()
        if not command.text and command.picture is None:
            self.chat.wait_for_text(Command("ticket", (kind.value,)))
            return Reply(
                _("Describe it in one message; its first line becomes the topic."),
                nav("ticket"),
                title=title,
            )
        try:
            conversation = self.requests.open(kind, command.text, _image(command))
        except TooManyRequestsError:
            return Reply(_("Too many requests in an hour. Please try again later."), nav())
        except ValidationError as error:
            return Reply(" ".join(error.messages), nav())
        return Reply(
            _("Request #%(number)s is open. The administrators answer you here.")
            % {"number": conversation.pk},
            (Button(_("My requests"), "requests"), *nav()),
            title=title,
        )

    def my_requests(self, command: Command) -> Reply:
        requests = list(self.requests.all().order_by("-updated_at")[:_LISTED])
        title = _("My requests")
        new_request = Button(_("New request"), "ticket")
        if not requests:
            return Reply(
                _("You have no requests yet."), (new_request, *nav("support")), title=title
            )
        lines = [f"#{r.pk} {_topic(r)}: {r.get_status_display()}" for r in requests]
        buttons = tuple(Button(f"#{r.pk} {_topic(r)}", f"request {r.pk}") for r in requests)
        return Reply(bullets(lines), (*buttons, new_request, *nav("support")), title=title)

    def request(self, command: Command) -> Reply:
        """The last messages of one request; reading them here marks the answers as seen."""
        conversation = self._find(command)
        if conversation is None:
            return unavailable()
        messages = list(self.requests.messages(conversation).order_by("-pk")[:_SHOWN_MESSAGES])
        self.requests.mark_read(conversation)
        lines = [
            f"{_topic(conversation)}: {conversation.get_status_display()}",
            *(self._line(message.author_id, message.text) for message in reversed(messages)),
        ]
        buttons = (
            (Button(_("Answer"), f"answer {conversation.pk}"),) if conversation.is_open else ()
        )
        return Reply(
            "\n\n".join(lines),
            (*buttons, *nav("requests")),
            title=_("Request #%(number)s") % {"number": conversation.pk},
        )

    def answer(self, command: Command) -> Reply:
        """`answer <id>`: wait for the answer; with the typed answer: add it to the request."""
        conversation = self._find(command)
        if conversation is None:
            return unavailable()
        if not command.text and command.picture is None:
            self.chat.wait_for_text(Command("answer", (str(conversation.pk),)))
            return Reply(_("Write your answer in one message."), nav(f"request {conversation.pk}"))
        try:
            self.requests.reply(conversation, command.text, _image(command))
        except ClosedConversationError:
            return Reply(
                _("This request is closed. Open a new one if you need to."),
                (Button(_("New request"), "ticket"), *nav()),
            )
        except TooManyMessagesError:
            return Reply(_("Too many messages in a minute. Please wait a little."), nav())
        except ValidationError as error:
            return Reply(" ".join(error.messages), nav())
        return Reply(_("Sent to the administrators."), nav(f"request {conversation.pk}"))

    def _find(self, command: Command) -> Conversation | None:
        """Only among the user's own requests."""
        pk = number(command.arg(0))
        return None if pk is None else self.requests.all().filter(pk=pk).first()

    def _line(self, author_id: int | None, text: str) -> str:
        who = _("You") if author_id == self.user.pk else _("Support")
        return f"{who}: {text[:_MESSAGE_LENGTH] or '…'}"


def _image(command: Command) -> Upload | None:
    """The picture of the message as an upload; the support checks it is an image."""
    picture = command.picture
    if picture is None:
        return None
    return SimpleUploadedFile(picture.name, picture.content, picture.mime_type)


def _topic(conversation: Conversation) -> str:
    return conversation.subject or str(conversation.get_type_display())
