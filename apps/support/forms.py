from typing import Any, ClassVar, cast

from django import forms
from django.db import models
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Status, User
from apps.dashboard.services.users import users_on_server
from apps.plans.models import Plan
from apps.servers.models import Server
from apps.support.models import Conversation, Message, ReplyTemplate, TicketStatus, TicketType
from apps.support.services.images import validate_image
from apps.support.services.inbox import staff

_TEXT_LENGTH = 5000
_IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp"


class _ImageField(forms.FileField):
    widget = forms.ClearableFileInput(attrs={"accept": _IMAGE_TYPES})

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value:
            validate_image(value)


class RequestForm(forms.Form):
    """A request of any type but "server does not work", which has its own quick form."""

    type = forms.ChoiceField(
        label=_("What happened"),
        choices=[(t.value, t.label) for t in TicketType if t != TicketType.SERVER_DOWN],
    )
    text = forms.CharField(
        label=_("Description"), max_length=_TEXT_LENGTH, widget=forms.Textarea(attrs={"rows": 5})
    )
    image = _ImageField(label=_("Screenshot"), required=False)

    @property
    def ticket_type(self) -> TicketType:
        return TicketType(self.cleaned_data["type"])


class ServerDownForm(forms.Form):
    server = forms.ModelChoiceField(queryset=Server.objects.all(), widget=forms.HiddenInput)
    comment = forms.CharField(
        label=_("Comment"),
        required=False,
        max_length=_TEXT_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class ReplyForm(forms.Form):
    """The reply box of a chat (support/_composer.html): one growing line and a screenshot clip."""

    text = forms.CharField(
        label=_("Message"),
        required=False,
        max_length=_TEXT_LENGTH,
        widget=forms.Textarea(
            attrs={
                "rows": 1,
                "class": "composer-input",
                "placeholder": _("Message"),
                "data-autogrow": "",
                "data-enter-send": "",
            }
        ),
    )
    image = _ImageField(
        label=_("Screenshot"),
        required=False,
        widget=forms.FileInput(attrs={"accept": _IMAGE_TYPES, "class": "sr-only"}),
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not data.get("text", "").strip() and not data.get("image"):
            raise forms.ValidationError(_("Write a message or attach a screenshot."))
        return data


class Sort(models.TextChoices):
    WAITING = "waiting", _("Waiting for an answer first")
    UNREAD = "unread", _("Unread first")
    RECENT = "recent", _("Last message")
    NEWEST = "newest", _("Newest")
    OLDEST = "oldest", _("Oldest")


# The id breaks ties: conversations touched in the same instant keep a fixed order.
_ORDER = {
    Sort.WAITING: ("-waiting", "-unread", "-updated_at", "-pk"),
    Sort.UNREAD: ("-unread", "-updated_at", "-pk"),
    Sort.RECENT: ("-updated_at", "-pk"),
    Sort.NEWEST: ("-created_at", "-pk"),
    Sort.OLDEST: ("created_at", "pk"),
}

# Assignee filter value for requests nobody took.
NOBODY = "none"


class _InboxFilterForm(forms.Form):
    """Filters both lists share; a wrong value is ignored instead of an error page."""

    search = forms.CharField(label=_("User or text"), required=False)
    plan = forms.ModelChoiceField(
        label=_("Plan"), queryset=Plan.objects.all(), required=False, empty_label=_("Any")
    )
    sort = forms.ChoiceField(label=_("Sort"), required=False)
    sorts: ClassVar[tuple[Sort, ...]] = (Sort.UNREAD, Sort.RECENT, Sort.NEWEST, Sort.OLDEST)
    default_sort = Sort.UNREAD

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices = [(sort.value, sort.label) for sort in self.sorts]
        cast(forms.ChoiceField, self.fields["sort"]).choices = choices

    def filter(self, conversations: QuerySet[Conversation]) -> QuerySet[Conversation]:
        data = self.cleaned_data if self.is_valid() else {}
        if term := data.get("search"):
            texts = Message.objects.filter(internal=False, text__icontains=term)
            conversations = conversations.filter(
                Q(user__username__icontains=term)
                | Q(user__email__icontains=term)
                | Q(pk__in=texts.values("conversation_id"))
            )
        if data.get("plan"):
            conversations = conversations.filter(user__limits__plan=data["plan"])
        conversations = self.filter_more(conversations, data)
        return conversations.order_by(*_ORDER[Sort(data.get("sort") or self.default_sort)])

    def filter_more(
        self, conversations: QuerySet[Conversation], data: dict[str, Any]
    ) -> QuerySet[Conversation]:
        return conversations


class ChatFilterForm(_InboxFilterForm):
    """Chats that wait for an answer come first."""

    sorts = (Sort.WAITING, *_InboxFilterForm.sorts)
    default_sort = Sort.WAITING

    server = forms.ModelChoiceField(
        label=_("Server"),
        queryset=Server.objects.order_by("name"),
        required=False,
        empty_label=_("Any"),
        help_text=_("Users with a key on the server."),
    )
    status = forms.ChoiceField(
        label=_("User status"), choices=[("", _("Any")), *Status.choices], required=False
    )

    def filter_more(
        self, conversations: QuerySet[Conversation], data: dict[str, Any]
    ) -> QuerySet[Conversation]:
        if data.get("server"):
            users = users_on_server(User.objects.all(), data["server"])
            conversations = conversations.filter(user__in=users)
        if data.get("status"):
            conversations = conversations.filter(user__status=data["status"])
        return conversations


class RequestFilterForm(_InboxFilterForm):
    type = forms.ChoiceField(
        label=_("Type"), choices=[("", _("Any")), *TicketType.choices], required=False
    )
    status = forms.ChoiceField(
        label=_("Status"), choices=[("", _("Any")), *TicketStatus.choices], required=False
    )
    server = forms.ModelChoiceField(
        label=_("Server"),
        queryset=Server.objects.order_by("name"),
        required=False,
        empty_label=_("Any"),
    )
    assignee = forms.ChoiceField(label=_("Assignee"), required=False)
    since = forms.DateField(
        label=_("Opened from"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    until = forms.DateField(
        label=_("Opened till"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    default_sort = Sort.NEWEST

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        members = [(str(member.pk), member.username) for member in staff()]
        cast(forms.ChoiceField, self.fields["assignee"]).choices = [
            ("", _("Any")),
            (NOBODY, _("Nobody")),
            *members,
        ]

    def filter_more(
        self, conversations: QuerySet[Conversation], data: dict[str, Any]
    ) -> QuerySet[Conversation]:
        lookups = {
            "type": data.get("type"),
            "status": data.get("status"),
            "server": data.get("server"),
            "created_at__date__gte": data.get("since"),
            "created_at__date__lte": data.get("until"),
        }
        conversations = conversations.filter(**{k: v for k, v in lookups.items() if v})
        if data.get("assignee") == NOBODY:
            return conversations.filter(assignee=None)
        if data.get("assignee"):
            return conversations.filter(assignee_id=data["assignee"])
        return conversations


class StaffReplyForm(ReplyForm):
    internal = forms.BooleanField(
        label=_("Internal note"),
        required=False,
        help_text=_("Only the staff see it, never the user."),
    )

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        if data.get("internal") and data.get("image"):
            raise forms.ValidationError(_("An internal note takes no screenshot."))
        return data


class ManageForm(forms.Form):
    """Status and assignee of a request; only an administrator gets the assignee field."""

    status = forms.ChoiceField(label=_("Status"), choices=TicketStatus.choices)
    assignee = forms.ModelChoiceField(
        label=_("Assignee"), queryset=staff(), required=False, empty_label=_("Nobody")
    )

    def __init__(self, *args: Any, can_assign: bool, owner: User, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not can_assign:
            del self.fields["assignee"]
        else:
            cast(forms.ModelChoiceField, self.fields["assignee"]).queryset = staff(owner)  # type: ignore[type-arg]


class CloseIncidentForm(forms.Form):
    text = forms.CharField(
        label=_("Answer to everyone who reported"),
        required=False,
        max_length=_TEXT_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ReplyTemplateForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = ReplyTemplate
        fields = ("title", "text")
        widgets: ClassVar = {"text": forms.Textarea(attrs={"rows": 6})}
