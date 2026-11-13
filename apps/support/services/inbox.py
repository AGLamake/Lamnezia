"""The staff's inbox (DESIGN.md 6.7): chats and requests with unread counts, answers, internal
notes, assignees and statuses.
"""

from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    When,
)
from django.utils import timezone

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.accounts.services.zone import Zone
from apps.support.models import Conversation, Kind, Message, TicketStatus
from apps.support.services import live
from apps.support.services.images import Upload
from apps.support.services.relay import tell_of_answer, tell_of_status
from apps.support.services.thread import post
from apps.support.services.tickets import ClosedConversationError


class NotAllowedError(Exception):
    pass


def staff(owner: User | None = None) -> QuerySet[User]:
    """Who a conversation can be assigned to: of the moderators, only the one serving `owner`."""
    members = User.objects.filter(role__in=STAFF_ROLES)
    if owner is not None:
        members = members.filter(Q(role=Role.ADMIN) | Q(pk=owner.manager_id))
    return members.order_by("username")


def _from_user() -> Q:
    """Messages the user of the thread wrote, which the staff are to read."""
    return Q(internal=False, author=F("conversation__user"))


class Inbox:
    """Conversations as one staff member sees them.

    An administrator sees every conversation. A moderator sees the conversations of the users
    they serve that are assigned to them or to nobody: never anything outside their zone, so
    neither its messages nor its attachments.
    """

    def __init__(self, member: User) -> None:
        self._member = member

    def visible(self) -> QuerySet[Conversation]:
        conversations = Zone(self._member).narrow(Conversation.objects.all(), user="user")
        if self._member.is_admin:
            return conversations
        return conversations.filter(Q(assignee=self._member) | Q(assignee=None))

    def conversations(self) -> QuerySet[Conversation]:
        """Visible conversations; `unread` is the number of the user's messages nobody has seen."""
        unread = Q(messages__read_at=None, messages__internal=False, messages__author=F("user"))
        return (
            self.visible()
            .select_related("user__limits__plan", "server", "assignee")
            .annotate(unread=Count("messages", filter=unread))
        )

    def chats(self) -> QuerySet[Conversation]:
        """Chats someone wrote in: one with nothing but lines about requests is left out.

        Each carries its last message the user sees (`last_text`, `last_author_id`) and
        `waiting`: the user wrote last, so the chat waits for an answer.
        """
        written = Message.objects.filter(conversation=OuterRef("pk"), event="")
        last = written.filter(internal=False).order_by("-created_at", "-pk")
        return (
            self.conversations()
            .filter(Exists(written), kind=Kind.CHAT)
            .annotate(
                last_text=Subquery(last.values("text")[:1]),
                last_author_id=Subquery(last.values("author")[:1]),
            )
            .annotate(
                waiting=Case(
                    When(last_author_id=F("user_id"), then=True),
                    default=False,
                    output_field=BooleanField(),
                )
            )
        )

    def requests(self) -> QuerySet[Conversation]:
        return self.conversations().filter(kind=Kind.TICKET)

    def unread(self) -> int:
        """Unread messages in all visible conversations, for the menu counter."""
        visible = self.visible()
        messages = Message.objects.filter(_from_user(), read_at=None, conversation__in=visible)
        return messages.count()

    def messages(self, conversation: Conversation) -> QuerySet[Message]:
        """The whole thread, internal notes included."""
        return conversation.messages.select_related("author").prefetch_related("attachments")

    def mark_read(self, conversation: Conversation) -> None:
        seen = conversation.messages.filter(
            internal=False, author=conversation.user_id, read_at=None
        ).update(read_at=timezone.now())
        if seen:
            live.thread_changed(conversation)
            live.unread_changed(conversation, staff=True)

    def reply(self, conversation: Conversation, text: str, image: Upload | None = None) -> Message:
        """An answer the user sees; a new request goes into work with it."""
        if not conversation.is_open:
            raise ClosedConversationError(conversation.pk)
        message = post(conversation, self._member, text, image)
        if conversation.kind == Kind.TICKET and conversation.status == TicketStatus.NEW:
            self.set_status(conversation, TicketStatus.IN_PROGRESS)
        tell_of_answer(conversation, message)
        return message

    def note(self, conversation: Conversation, text: str) -> Message:
        """An internal note: only the staff ever see it."""
        return post(conversation, self._member, text, internal=True)

    def set_status(self, conversation: Conversation, status: TicketStatus) -> None:
        changed = conversation.status != status
        conversation.status = status
        conversation.save(update_fields=["status"])
        live.thread_changed(conversation)
        if changed:
            tell_of_status(conversation)

    def assign(self, conversation: Conversation, assignee: User | None) -> None:
        """Only an administrator hands conversations out; `None` takes the assignee off."""
        if not self._member.is_admin:
            raise NotAllowedError
        conversation.assignee = assignee
        conversation.save(update_fields=["assignee"])
        live.unread_changed(conversation, staff=True)
