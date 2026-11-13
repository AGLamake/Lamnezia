"""Forms of the guides: a user's suggestion and the staff's publish and decline forms."""

from typing import Any, ClassVar
from urllib.parse import urlsplit

from django import forms
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.guides.models import (
    FaqArticle,
    MaterialLanguage,
    Suggestion,
    SuggestionKind,
    UsefulMaterial,
)


class SuggestionForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = Suggestion
        fields = ("kind", "title", "url", "body", "language", "topic", "comment", "credit_author")
        widgets: ClassVar = {
            "kind": forms.RadioSelect,
            "body": forms.Textarea(attrs={"rows": 12}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class MaterialPublishForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = UsefulMaterial
        fields = (
            "slug",
            "title",
            "url",
            "language",
            "source",
            "topic",
            "description_ru",
            "description_en",
            "credit",
        )


class ArticlePublishForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = FaqArticle
        fields = ("slug", "title_ru", "body_ru", "title_en", "body_en", "credit")
        widgets: ClassVar = {
            "body_ru": forms.Textarea(attrs={"rows": 12}),
            "body_en": forms.Textarea(attrs={"rows": 12}),
        }


PublishForm = MaterialPublishForm | ArticlePublishForm


class PublishAs(models.TextChoices):
    """What the staff make of a suggestion, whatever the author sent."""

    MATERIAL = "material", _("a useful material")
    ARTICLE = "article", _("an FAQ article")

    @classmethod
    def of(cls, suggestion: Suggestion, chosen: str | None = None) -> "PublishAs":
        """The staff's choice; without one, a link becomes a material and a text an article."""
        if chosen in cls.values:
            return cls(chosen)
        return cls.MATERIAL if suggestion.kind == SuggestionKind.LINK else cls.ARTICLE


def publish_form(
    suggestion: Suggestion, data: Any = None, target: str | None = None
) -> PublishForm:
    """The form for what the suggestion becomes (`PublishAs.of`); the fields start from the
    suggestion and the author is named only if they asked for it.
    """
    initial: dict[str, Any] = {
        "slug": slugify(suggestion.title) or f"suggestion-{suggestion.pk}",
        "credit": suggestion.author_name if suggestion.credit_author else "",
    }
    suffix = "en" if suggestion.language == MaterialLanguage.EN else "ru"
    if PublishAs.of(suggestion, target) == PublishAs.MATERIAL:
        initial |= {
            "title": suggestion.title,
            "url": suggestion.url,
            "source": (urlsplit(suggestion.url).hostname or "").removeprefix("www."),
            "language": suggestion.language,
            "topic": suggestion.topic,
            f"description_{suffix}": suggestion.body,
        }
        return MaterialPublishForm(data, initial=initial)
    body = suggestion.body or f"[{suggestion.title}]({suggestion.url})"
    initial |= {f"title_{suffix}": suggestion.title, f"body_{suffix}": body}
    return ArticlePublishForm(data, initial=initial)


class RejectForm(forms.Form):
    reason = forms.CharField(
        label=_("Reason"),
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("The author gets it in the notification."),
    )
