"""The user's chat with the staff (DESIGN.md 6.7): one per user, opened by the first message or
by the first line about a request.
"""

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.support.models import Conversation, Kind, Message, TicketEvent
from apps.support.services import live
from apps.support.services.images import Upload
from apps.support.services.thread import MyThreads

_PREVIEW_LENGTH = 200


class MyChat(MyThreads):
    def all(self) -> QuerySet[Conversation]:
        return super().all().filter(kind=Kind.CHAT)

    def get(self) -> Conversation | None:
        return self.all().first()

    def send(self, text: str, image: Upload | None = None) -> Message:
        """A message to the staff; the routed channels hear of it (Event.CHAT_MESSAGE)."""
        chat = self._open()
        message = self._post(chat, text, image)
        emit(
            Event.CHAT_MESSAGE,
            {
                "user": self._user.username,
                "number": chat.pk,
                "text": message.text[:_PREVIEW_LENGTH],
            },
            user=self._user,
        )
        return message

    def tell(self, ticket: Conversation, event: TicketEvent) -> Message:
        """A line about the user's request `ticket`: a link to it, never the text of an answer.

        It is seen at once (the request itself counts as unread) and does not move the chat up
        in the staff's inbox.
        """
        assert ticket.user_id == self._user.pk and ticket.kind == Kind.TICKET
        chat = self._open()
        line = Message.objects.create(
            conversation=chat, event=event, ticket=ticket, read_at=timezone.now()
        )
        live.thread_changed(chat)
        return line

    def _open(self) -> Conversation:
        if (chat := self.get()) is not None:
            return chat
        try:
            with transaction.atomic():
                return Conversation.objects.create(user=self._user, kind=Kind.CHAT)
        except IntegrityError:
            # Two first messages at once: the other one opened the chat.
            return self.all().get()
