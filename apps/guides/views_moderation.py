"""Review of article suggestions for administrators and moderators."""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.permissions import StaffRequiredMixin
from apps.guides.forms import PublishAs, PublishForm, RejectForm, publish_form
from apps.guides.models import Suggestion, SuggestionKind, SuggestionStatus
from apps.guides.services.markdown import render_markdown
from apps.guides.services.suggestions import SuggestionReview

_RECENT_DECISIONS = 20


class _ModerationView(StaffRequiredMixin):
    def pending(self, pk: int) -> Suggestion:
        """A suggestion still waiting for review: a decided one is a 404, never decided twice."""
        return get_object_or_404(Suggestion.objects.pending().select_related("author"), pk=pk)

    def review(self) -> SuggestionReview:
        return SuggestionReview(self.member, ip=self.client_ip)


class SuggestionQueueView(StaffRequiredMixin, TemplateView):
    template_name = "guides/moderation/queue.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        suggestions = Suggestion.objects.select_related("author", "reviewed_by")
        return super().get_context_data(
            pending=suggestions.pending(),
            decided=suggestions.exclude(status=SuggestionStatus.PENDING)[:_RECENT_DECISIONS],
            **kwargs,
        )


class SuggestionReviewView(_ModerationView, TemplateView):
    """The suggestion as the author sent it, the publish form to edit it in and the decline form."""

    template_name = "guides/moderation/review.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        suggestion = self.pending(pk)
        target = PublishAs.of(suggestion, request.GET.get("as"))
        return self.page(suggestion, target, publish_form(suggestion, target=target), RejectForm())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        suggestion = self.pending(pk)
        target = PublishAs.of(suggestion, request.POST.get("as"))
        form = publish_form(suggestion, request.POST, target)
        if not form.is_valid():
            return self.page(suggestion, target, form, RejectForm())
        self.review().publish(suggestion, form.save(commit=False))
        messages.success(request, _("Published. The author is notified."))
        return redirect("guides:moderation")

    def page(
        self, suggestion: Suggestion, target: PublishAs, publish: PublishForm, reject: RejectForm
    ) -> HttpResponse:
        preview = render_markdown(suggestion.body) if suggestion.kind == SuggestionKind.TEXT else ""
        return self.render_to_response(
            self.get_context_data(
                suggestion=suggestion,
                preview=preview,
                target=target,
                targets=PublishAs,
                publish_form=publish,
                reject_form=reject,
            )
        )


class SuggestionRejectView(_ModerationView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        suggestion = self.pending(pk)
        form = RejectForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("Give the reason: the author gets it."))
            return redirect("guides:moderation-review", pk=pk)
        self.review().reject(suggestion, form.cleaned_data["reason"])
        messages.success(request, _("Declined. The author is notified."))
        return redirect("guides:moderation")
