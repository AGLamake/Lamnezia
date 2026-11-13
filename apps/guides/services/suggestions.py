"""Article suggestions of users: sending one and the staff's review, publish or decline."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.core.services.site_settings import cached_settings
from apps.guides.models import (
    FaqArticle,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    UsefulMaterial,
)
from apps.notifications.events import Event, english
from apps.notifications.services.delivery import emit

Published = UsefulMaterial | FaqArticle


class SuggestionsClosedError(Exception):
    """An administrator turned suggestions off in the site settings."""


class TooManySuggestionsError(Exception):
    pass


def accepting_suggestions() -> bool:
    return cached_settings.get().guide_suggestions


class MySuggestions:
    """The suggestions of one user: sending a new one and seeing what became of the old ones."""

    def __init__(self, user: User) -> None:
        self._user = user

    def all(self) -> QuerySet[Suggestion]:
        return Suggestion.objects.filter(author=self._user)

    def send(self, suggestion: Suggestion) -> Suggestion:
        """Saves the suggestion as the user's and tells the staff channels about it."""
        if not accepting_suggestions():
            raise SuggestionsClosedError
        self._throttle()
        suggestion.author = self._user
        with transaction.atomic():
            suggestion.save()
            emit(
                Event.SUGGESTION_CREATED,
                {
                    "user": self._user.get_username(),
                    "kind": english(SuggestionKind(suggestion.kind).label),
                    "title": suggestion.title,
                },
                key=f"suggestion-{suggestion.pk}",
                user=self._user,
            )
        return suggestion

    def _throttle(self) -> None:
        """At most SUGGESTION_RATE_LIMIT suggestions a day (0: no limit)."""
        limit = settings.SUGGESTION_RATE_LIMIT
        since = timezone.now() - timedelta(days=1)
        if limit and self.all().filter(created_at__gte=since).count() >= limit:
            raise TooManySuggestionsError


class SuggestionReview:
    """Decisions of one staff member; each goes to the audit journal and to the author."""

    def __init__(self, reviewer: User, ip: str | None = None) -> None:
        self._reviewer = reviewer
        self._ip = ip

    def publish(self, suggestion: Suggestion, published: Published) -> Published:
        """Saves the edited material or FAQ article and links the suggestion to it."""
        with transaction.atomic():
            published.save()
            if isinstance(published, UsefulMaterial):
                suggestion.material = published
                path = reverse("guides:materials")
            else:
                suggestion.article = published
                path = reverse("guides:faq-article", args=[published.slug])
            self._decide(suggestion, SuggestionStatus.ACCEPTED)
            self._audit("guides.suggestion_accept", suggestion, published=published.slug)
            emit(
                Event.SUGGESTION_ACCEPTED,
                {"title": suggestion.title, "url": settings.SITE_URL.rstrip("/") + path},
                key=f"suggestion-{suggestion.pk}-decision",
                user=suggestion.author,
            )
        return published

    def reject(self, suggestion: Suggestion, reason: str) -> None:
        with transaction.atomic():
            suggestion.reject_reason = reason
            self._decide(suggestion, SuggestionStatus.REJECTED)
            self._audit("guides.suggestion_reject", suggestion, reason=reason)
            emit(
                Event.SUGGESTION_REJECTED,
                {"title": suggestion.title, "reason": reason},
                key=f"suggestion-{suggestion.pk}-decision",
                user=suggestion.author,
            )

    def _decide(self, suggestion: Suggestion, status: SuggestionStatus) -> None:
        suggestion.status = status
        suggestion.reviewed_by = self._reviewer
        suggestion.reviewed_at = timezone.now()
        suggestion.save()

    def _audit(self, action: str, suggestion: Suggestion, **data: str) -> None:
        data["author"] = suggestion.author.get_username()
        audit.log(action, actor=self._reviewer, target=suggestion, data=data, ip=self._ip)
