"""Requests of a user to the staff (DESIGN.md 6.7): opening one, the quick "server does not work"
report and the replies of the thread.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.events import Event, english
from apps.notifications.services.delivery import emit
from apps.plans.services.limits import effective_limits
from apps.servers.models import Server
from apps.support.models import (
    OPEN_STATUSES,
    Conversation,
    Kind,
    Message,
    TicketStatus,
    TicketType,
)
from apps.support.services.check import start_check
from apps.support.services.context import snapshot
from apps.support.services.images import Upload
from apps.support.services.incidents import add_report
from apps.support.services.thread import MyThreads, post

_SUBJECT_LENGTH = 80


class TooManyRequestsError(Exception):
    pass


class ServerNotAllowedError(Exception):
    pass


class ClosedConversationError(Exception):
    pass


class MyRequests(MyThreads):
    """The requests of one user; the user sees none of the staff's internal notes."""

    def __init__(self, user: User, user_agent: str = "", via: str = "") -> None:
        super().__init__(user, via)
        self._user_agent = user_agent

    def all(self) -> QuerySet[Conversation]:
        return super().all().filter(kind=Kind.TICKET).select_related("server")

    def open(self, type: TicketType, text: str, image: Upload | None = None) -> Conversation:
        """A request of any type; the first line of the text is its subject."""
        subject = text.strip().splitlines()[0][:_SUBJECT_LENGTH] if text.strip() else ""
        return self._open(type, subject, text, image)

    def report_server(self, server: Server, comment: str = "") -> Conversation:
        """The one-click report: starts a check of the server and keeps its result on the request.

        A user who already reported the server and is still waiting gets that request back.
        """
        if not effective_limits(self._user).allows_server(server.pk):
            raise ServerNotAllowedError(server.pk)
        existing = self.all().filter(
            type=TicketType.SERVER_DOWN, server=server, status__in=OPEN_STATUSES
        )
        if (reported := existing.first()) is not None:
            if comment.strip():
                self.reply(reported, comment)
            return reported
        conversation = self._open(TicketType.SERVER_DOWN, server.name, comment, server=server)
        add_report(conversation)
        start_check(conversation, self._user)
        return conversation

    def reply(self, conversation: Conversation, text: str, image: Upload | None = None) -> Message:
        if not conversation.is_open:
            raise ClosedConversationError(conversation.pk)
        with transaction.atomic():
            message = self._post(conversation, text, image)
            if conversation.status == TicketStatus.WAITING_USER:
                conversation.status = TicketStatus.IN_PROGRESS
                conversation.save(update_fields=["status"])
        return message

    def _open(
        self,
        type: TicketType,
        subject: str,
        text: str,
        image: Upload | None = None,
        server: Server | None = None,
    ) -> Conversation:
        self._throttle()
        with transaction.atomic():
            conversation = Conversation.objects.create(
                user=self._user,
                kind=Kind.TICKET,
                type=type,
                subject=subject,
                server=server,
                context=snapshot(self._user, self._user_agent),
            )
            if text.strip() or image:
                post(conversation, self._user, text, image, via=self._via)
            emit(
                Event.TICKET_CREATED,
                {
                    "user": self._user.username,
                    "number": conversation.pk,
                    "type": english(type.label),
                    "subject": subject,
                },
                user=self._user,
            )
        return conversation

    def _throttle(self) -> None:
        """At most SUPPORT_RATE_LIMIT new requests an hour (0: no limit)."""
        limit = settings.SUPPORT_RATE_LIMIT
        since = timezone.now() - timedelta(hours=1)
        if limit and self.all().filter(created_at__gte=since).count() >= limit:
            raise TooManyRequestsError
