"""The staff's inbox: chats, requests, one conversation and incidents (DESIGN.md 6.7)."""

from functools import cached_property
from typing import Any

from django.contrib import messages
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import ListView

from apps.accounts.permissions import AdminRequiredMixin, StaffRequiredMixin
from apps.support.forms import (
    ChatFilterForm,
    CloseIncidentForm,
    ManageForm,
    RequestFilterForm,
    StaffReplyForm,
)
from apps.support.models import Conversation, Incident, Kind, ReplyTemplate, TicketStatus
from apps.support.services.check import CheckReport
from apps.support.services.client import ClientCard
from apps.support.services.inbox import Inbox
from apps.support.services.incidents import close_incident, incidents
from apps.support.services.tickets import ClosedConversationError

_PAGE_SIZE = 50


class _InboxView(StaffRequiredMixin):
    @property
    def inbox(self) -> Inbox:
        return Inbox(self.member)


class ChatListView(_InboxView, ListView):  # type: ignore[type-arg]
    template_name = "support/inbox/chats.html"
    paginate_by = _PAGE_SIZE

    def get_queryset(self) -> QuerySet[Conversation]:
        self.filter_form = ChatFilterForm(self.request.GET or None)
        return self.filter_form.filter(self.inbox.chats())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(filter_form=self.filter_form, **kwargs)


class RequestListView(_InboxView, ListView):  # type: ignore[type-arg]
    template_name = "support/inbox/requests.html"
    paginate_by = _PAGE_SIZE

    def get_queryset(self) -> QuerySet[Conversation]:
        self.filter_form = RequestFilterForm(self.request.GET or None)
        return self.filter_form.filter(self.inbox.requests())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(filter_form=self.filter_form, **kwargs)


class _ConversationView(_InboxView):
    kwargs: dict[str, Any]

    @cached_property
    def conversation(self) -> Conversation:
        """A conversation the member may not see is as good as missing."""
        return get_object_or_404(self.inbox.conversations(), pk=self.kwargs["pk"])


class ConversationView(_ConversationView, View):
    """The thread with internal notes; a reply sent by HTMX gets the thread back."""

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        self.inbox.mark_read(self.conversation)
        context = {
            "conversation": self.conversation,
            "report": CheckReport.from_json(self.conversation.server_check),
            "manage_form": ManageForm(
                initial={
                    "status": self.conversation.status,
                    "assignee": self.conversation.assignee,
                },
                can_assign=self.member.is_admin,
                owner=self.conversation.user,
            ),
            "client": self.client_card(),
            **self.thread_context(StaffReplyForm()),
        }
        return render(request, "support/inbox/conversation.html", context)

    def client_card(self) -> ClientCard | None:
        """A chat has no context saved when it opened: the card shows the user as they are."""
        if self.conversation.kind != Kind.CHAT:
            return None
        return ClientCard.of(self.conversation.user, self.inbox)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        form = StaffReplyForm(request.POST, request.FILES)
        if form.is_valid():
            self.send(form.cleaned_data)
            form = StaffReplyForm()
        if not request.headers.get("HX-Request"):
            return redirect("support:inbox-conversation", pk=pk)
        return render(request, "support/inbox/_thread.html", self.thread_context(form))

    def send(self, data: dict[str, Any]) -> None:
        if data["internal"]:
            self.inbox.note(self.conversation, data["text"])
            return
        try:
            self.inbox.reply(self.conversation, data["text"], data["image"])
        except ClosedConversationError:
            messages.error(self.request, _("The request is closed. Open it again to answer."))

    def thread_context(self, form: StaffReplyForm) -> dict[str, Any]:
        return {
            **messages_context(self.conversation, self.inbox),
            "form": form,
            "templates": ReplyTemplate.objects.all(),
        }


def messages_context(conversation: Conversation, inbox: Inbox) -> dict[str, Any]:
    return {
        "conversation": conversation,
        "thread": inbox.messages(conversation),
        "messages_url": reverse("support:inbox-messages", args=[conversation.pk]),
    }


class MessagesView(_ConversationView, View):
    """The messages of a thread alone: what an open page fetches when the thread changes."""

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        self.inbox.mark_read(self.conversation)
        context = messages_context(self.conversation, self.inbox)
        return render(request, "support/inbox/_messages.html", context)


class UnreadView(_InboxView, View):
    """The menu counter of the inbox; polled while the page has no WebSocket."""

    def get(self, request: HttpRequest) -> HttpResponse:
        context = {"unread": self.inbox.unread(), "unread_url": request.path}
        return render(request, "support/_badge.html", context)


class ManageView(_ConversationView, View):
    """Status of a request and, for an administrator, its assignee."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        form = ManageForm(
            request.POST, can_assign=self.member.is_admin, owner=self.conversation.user
        )
        if form.is_valid():
            self.inbox.set_status(self.conversation, TicketStatus(form.cleaned_data["status"]))
            if "assignee" in form.fields:
                self.inbox.assign(self.conversation, form.cleaned_data["assignee"])
            messages.success(request, _("Saved."))
        return redirect("support:inbox-conversation", pk=pk)


class IncidentListView(AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    template_name = "support/inbox/incidents.html"
    paginate_by = _PAGE_SIZE

    def get_queryset(self) -> QuerySet[Incident]:
        return incidents()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(close_form=CloseIncidentForm(), **kwargs)


class IncidentCloseView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        incident = get_object_or_404(Incident.objects.select_related("server"), pk=pk)
        form = CloseIncidentForm(request.POST)
        if incident.is_open and form.is_valid():
            close_incident(incident, self.member, form.cleaned_data["text"])
            messages.success(request, _("The incident is closed; the users are told."))
        return redirect("support:incidents")
