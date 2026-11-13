import uuid
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Messenger
from apps.operations.models import Operation
from apps.servers.models import Server


class Kind(models.TextChoices):
    CHAT = "chat", _("Chat")
    TICKET = "ticket", _("Request")


class TicketType(models.TextChoices):
    BUG = "bug", _("Something is broken")
    SERVER_DOWN = "server_down", _("Server does not work")
    QUESTION = "question", _("Question")
    OTHER = "other", _("Other")


class TicketStatus(models.TextChoices):
    NEW = "new", _("new")
    IN_PROGRESS = "in_progress", _("in progress")
    WAITING_USER = "waiting_user", _("waiting for you")
    RESOLVED = "resolved", _("resolved")
    CLOSED = "closed", _("closed")


OPEN_STATUSES = (TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_USER)


class TicketEvent(models.TextChoices):
    """What a line in the user's chat says of a request: an answer or a status the user hears of.

    The statuses keep the values of `TicketStatus`.
    """

    ANSWER = "answer", _("support answered")
    WAITING_USER = "waiting_user", _("waiting for you")
    RESOLVED = "resolved", _("resolved")
    CLOSED = "closed", _("closed")


class Priority(models.TextChoices):
    LOW = "low", _("low")
    NORMAL = "normal", _("normal")
    HIGH = "high", _("high")


class Incident(models.Model):
    """Reports that one server does not work, grouped while they keep coming (DESIGN.md 6.7).

    While an incident is open, the status page shows its server as having problems.
    """

    server = models.ForeignKey(
        Server, verbose_name=_("server"), on_delete=models.CASCADE, related_name="incidents"
    )
    opened_at = models.DateTimeField(_("opened at"), auto_now_add=True)
    last_report_at = models.DateTimeField(_("last report"), auto_now_add=True)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("closed by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("incident")
        verbose_name_plural = _("incidents")
        ordering = ("-opened_at",)

    def __str__(self) -> str:
        return f"{self.server} {self.opened_at:%Y-%m-%d %H:%M}"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class Conversation(models.Model):
    """A thread between a user and the staff: the user's chat or one request (DESIGN.md 6.7).

    `context` is what the panel knew of the user when the request was opened (plan, keys, last
    handshakes, browser); `server_check` is the result of the check a "server does not work"
    request starts, filled in by `check_operation`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    kind = models.CharField(_("kind"), max_length=16, choices=Kind.choices, default=Kind.TICKET)
    type = models.CharField(_("type"), max_length=16, choices=TicketType.choices, blank=True)
    subject = models.CharField(_("topic"), max_length=200, blank=True)
    status = models.CharField(
        _("status"), max_length=16, choices=TicketStatus.choices, default=TicketStatus.NEW
    )
    priority = models.CharField(
        _("priority"), max_length=8, choices=Priority.choices, default=Priority.NORMAL
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("assignee"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_conversations",
    )
    server = models.ForeignKey(
        Server,
        verbose_name=_("server"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    incident = models.ForeignKey(
        Incident,
        verbose_name=_("incident"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    context = models.JSONField(_("context"), default=dict, blank=True)
    server_check = models.JSONField(_("server check"), default=dict, blank=True)
    check_operation = models.ForeignKey(
        Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(_("opened at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("last message"), auto_now_add=True)

    class Meta:
        verbose_name = _("conversation")
        verbose_name_plural = _("conversations")
        ordering = ("-updated_at",)
        indexes = (models.Index(fields=["user", "kind", "created_at"]),)
        constraints = (
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(kind=Kind.CHAT),
                name="support_one_chat_per_user",
            ),
        )

    def __str__(self) -> str:
        return f"#{self.pk} {self.subject}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class Message(models.Model):
    """A message of a thread. `internal` notes are for the staff only; no author is the panel.

    A message with an `event` is a line of the user's chat about their request `ticket`: it has
    no author and no text and is seen as soon as it is written.
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("author"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    text = models.TextField(_("text"), blank=True)
    internal = models.BooleanField(_("internal note"), default=False)
    # The messenger the user wrote it in; empty: the panel. The staff's answer goes back there.
    via = models.CharField(_("sent from"), max_length=16, choices=Messenger.choices, blank=True)
    event = models.CharField(
        _("request event"), max_length=16, choices=TicketEvent.choices, blank=True
    )
    ticket = models.ForeignKey(
        Conversation,
        verbose_name=_("request"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("sent at"), auto_now_add=True)
    read_at = models.DateTimeField(_("seen at"), null=True, blank=True)

    class Meta:
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        ordering = ("created_at", "pk")

    def __str__(self) -> str:
        return f"{self.conversation_id}: {self.text[:50]}"


def _attachment_path(attachment: "Attachment", filename: str) -> str:
    # A random name: the one the user gave never reaches the disk.
    return f"support/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


class Attachment(models.Model):
    """An image of a message, served only to whoever may read the message."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(_("file"), upload_to=_attachment_path)
    content_type = models.CharField(max_length=32)
    size = models.PositiveIntegerField(_("size"))

    class Meta:
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")

    def __str__(self) -> str:
        return str(self.file.name)


class ReplyTemplate(models.Model):
    """A ready answer the staff put into a reply with one click."""

    title = models.CharField(_("title"), max_length=100)
    text = models.TextField(_("text"))

    class Meta:
        verbose_name = _("reply template")
        verbose_name_plural = _("reply templates")
        ordering = ("title",)

    def __str__(self) -> str:
        return self.title
