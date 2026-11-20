"""Forms of the guides: a user's suggestion, the staff's publish and decline forms and the
panel's article and material forms.
"""

from typing import Any, ClassVar
from urllib.parse import urlsplit

from django import forms
from django.db import models
from django.urls import reverse
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
        fields: tuple[str, ...] = (
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
        fields: tuple[str, ...] = ("slug", "title_ru", "body_ru", "title_en", "body_en", "credit")
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


class ArticleForm(ArticlePublishForm):
    """An FAQ article edited in the panel; its texts get the Markdown preview button."""

    class Meta(ArticlePublishForm.Meta):
        fields = (*ArticlePublishForm.Meta.fields, "is_published")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in ("body_ru", "body_en"):
            self.fields[name].widget.attrs.update(
                {
                    "data-markdown-preview": reverse("guides:staff-preview"),
                    "data-preview-label": _("Preview"),
                    "data-preview-error": _("The preview failed. Save the article to see it."),
                    "data-preview-button": "btn btn-secondary btn-sm mt-2",
                    "data-preview-output": "markdown card mt-2",
                }
            )


class MaterialForm(MaterialPublishForm):
    """A useful material edited in the panel."""

    class Meta(MaterialPublishForm.Meta):
        fields = (*MaterialPublishForm.Meta.fields, "is_published")


EntryForm = ArticleForm | MaterialForm


def entry_form(model: type[models.Model]) -> type[EntryForm]:
    """The panel's form for an FAQ article or a useful material."""
    return ArticleForm if model is FaqArticle else MaterialForm


class RejectForm(forms.Form):
    reason = forms.CharField(
        label=_("Reason"),
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("The author gets it in the notification."),
    )
