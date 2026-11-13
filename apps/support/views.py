from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView, TemplateView

from apps.accounts.models import User
from apps.accounts.services.link_offer import LinkOffer
from apps.core.services.site_settings import cached_settings
from apps.support.forms import ReplyForm, RequestForm, ServerDownForm
from apps.support.models import Attachment, Conversation, Kind
from apps.support.services.chat import MyChat
from apps.support.services.check import CheckReport
from apps.support.services.inbox import Inbox
from apps.support.services.thread import MyThreads, TooManyMessagesError
from apps.support.services.tickets import (
    ClosedConversationError,
    MyRequests,
    ServerNotAllowedError,
    TooManyRequestsError,
)


class _SupportView(LoginRequiredMixin):
    """A page of the signed-in user's own requests."""

    request: HttpRequest

    @property
    def user(self) -> User:
        assert isinstance(self.request.user, User)
        return self.request.user

    @property
    def requests(self) -> MyRequests:
        return MyRequests(self.user, self.request.headers.get("User-Agent", ""))

    def too_many(self) -> HttpResponse:
        messages.error(self.request, _("Too many requests. Try again in an hour."))
        return redirect("support:index")


class RequestListView(_SupportView, TemplateView):
    template_name = "support/index.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(conversations=self.requests.all(), **kwargs)


class RequestCreateView(_SupportView, FormView):  # type: ignore[type-arg]
    template_name = "support/new.html"
    form_class = RequestForm

    def form_valid(self, form: RequestForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            conversation = self.requests.open(form.ticket_type, data["text"], data["image"])
        except TooManyRequestsError:
            return self.too_many()
        messages.success(self.request, _("The request is sent. We will answer here."))
        return redirect("support:conversation", pk=conversation.pk)


class ServerDownView(_SupportView, View):
    """The one-click report from the server status or key page."""

    def post(self, request: HttpRequest) -> HttpResponse:
        form = ServerDownForm(request.POST)
        if not form.is_valid():
            raise Http404
        try:
            conversation = self.requests.report_server(
                form.cleaned_data["server"], form.cleaned_data["comment"]
            )
        except ServerNotAllowedError:
            raise Http404 from None
        except TooManyRequestsError:
            return self.too_many()
        messages.success(request, _("Thanks! We are checking the server now."))
        return redirect("support:conversation", pk=conversation.pk)


class _ConversationView(_SupportView):
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.conversation = get_object_or_404(self.requests.all(), pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def check_context(self) -> dict[str, Any]:
        operation = self.conversation.check_operation
        return {
            "conversation": self.conversation,
            "report": CheckReport.from_json(self.conversation.server_check),
            "checking": operation is not None and not operation.is_finished,
            "check_failed": operation is not None and operation.can_retry,
            "poll_seconds": settings.SUPPORT_CHECK_POLL_SECONDS,
        }


def _too_often(form: ReplyForm) -> None:
    form.add_error(None, _("You send messages too often. Wait a minute."))


class ConversationView(_ConversationView, View):
    """The thread; a reply sent by HTMX gets the thread back, a plain form a redirect."""

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        self.requests.mark_read(self.conversation)
        return self.render("support/conversation.html", ReplyForm())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        form = ReplyForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                self.requests.reply(
                    self.conversation, form.cleaned_data["text"], form.cleaned_data["image"]
                )
                form = ReplyForm()
            except ClosedConversationError:
                messages.error(request, _("The request is closed. Open a new one."))
                form = ReplyForm()
            except TooManyMessagesError:
                _too_often(form)
        if not request.headers.get("HX-Request"):
            return redirect("support:conversation", pk=pk)
        return self.render("support/_thread.html", form)

    def render(self, template: str, form: ReplyForm) -> HttpResponse:
        context = {
            **self.check_context(),
            "thread": self.requests.messages(self.conversation),
            "form": form,
            "messages_url": reverse("support:messages", args=[self.conversation.pk]),
            "send_url": reverse("support:conversation", args=[self.conversation.pk]),
        }
        return render(self.request, template, context)


# The dialog of the chat widget, and the prefix of the chat's ids in it: a request page has a
# thread of its own.
CHAT_WIDGET_ID = "support-chat"
_CHAT_PREFIX = "chat-"


def _prefix(conversation: Conversation) -> str:
    return _CHAT_PREFIX if conversation.kind == Kind.CHAT else ""


class ChatView(_SupportView, View):
    """The user's chat with the staff in the widget of the portal pages (support/_chat_widget.html).

    The widget loads it when opened; the chat opens with the first message. The chat has no page
    of its own: its old address leads to the portal with the widget open.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        if not request.headers.get("HX-Request"):
            return redirect(reverse("portal:index") + "#" + CHAT_WIDGET_ID)
        chat = MyChat(self.user)
        if (conversation := chat.get()) is not None:
            chat.mark_read(conversation)
        return self.render(self.form())

    def post(self, request: HttpRequest) -> HttpResponse:
        form = self.form(request.POST, request.FILES)
        if form.is_valid():
            try:
                MyChat(self.user).send(form.cleaned_data["text"], form.cleaned_data["image"])
                form = self.form()
            except TooManyMessagesError:
                _too_often(form)
        if not request.headers.get("HX-Request"):
            return redirect("support:chat")
        return self.render(form)

    @staticmethod
    def form(data: Any = None, files: Any = None) -> ReplyForm:
        """The reply form with ids of its own: a request page has one too."""
        return ReplyForm(data, files, auto_id=f"{_CHAT_PREFIX}%s")

    def render(self, form: ReplyForm) -> HttpResponse:
        chat = MyChat(self.user)
        conversation = chat.get()
        context = {
            "conversation": conversation,
            "thread": chat.messages(conversation) if conversation else [],
            "form": form,
            "messages_url": reverse("support:messages", args=[conversation.pk])
            if conversation
            else "",
            "send_url": reverse("support:chat"),
            "prefix": _CHAT_PREFIX,
            **LinkOffer(self.user, cached_settings.get()).context(),
        }
        return render(self.request, "support/_thread.html", context)


class MessagesView(_SupportView, View):
    """The messages of a thread alone: what an open page fetches when the thread changes."""

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        threads = MyThreads(self.user)
        conversation = get_object_or_404(threads.all(), pk=pk)
        threads.mark_read(conversation)
        context = {
            "conversation": conversation,
            "thread": threads.messages(conversation),
            "messages_url": request.path,
            "prefix": _prefix(conversation),
        }
        return render(request, "support/_live_messages.html", context)


class UnreadView(_SupportView, View):
    """The Support tab's counter of unseen answers to requests; polled without a WebSocket."""

    threads: type[MyThreads] = MyRequests

    def get(self, request: HttpRequest) -> HttpResponse:
        context = {"unread": self.threads(self.user).unread(), "unread_url": request.path}
        return render(request, "support/_badge.html", context)


class ChatUnreadView(UnreadView):
    """The counter of unseen chat answers on the widget button."""

    threads = MyChat


class CheckStateView(_ConversationView, View):
    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return render(request, "support/_check.html", self.check_context())


class AttachmentView(LoginRequiredMixin, View):
    """Images are never public: the owner of the request sees the ones not in internal notes,
    staff those of the conversations their inbox shows."""

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        attachment = get_object_or_404(
            Attachment.objects.select_related("message__conversation"), pk=pk
        )
        message = attachment.message
        user = request.user
        assert isinstance(user, User)
        owner = message.conversation.user_id == user.pk and not message.internal
        staff = (
            user.is_staff_member
            and Inbox(user).visible().filter(pk=message.conversation_id).exists()
        )
        if not (owner or staff):
            raise PermissionDenied
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.content_type)
        response["Content-Disposition"] = "inline"
        response["X-Content-Type-Options"] = "nosniff"
        return response
