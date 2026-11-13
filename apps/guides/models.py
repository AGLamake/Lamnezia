from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

# Links people follow from the panel: no javascript:, data: or ftp: addresses.
http_url = URLValidator(schemes=["http", "https"])
_CREDIT_HELP = _("Who suggested it; shown under the text. Empty: nobody is named.")


if TYPE_CHECKING:
    _URLField = models.URLField[str, str]
else:
    _URLField = models.URLField


class LinkField(_URLField):
    """A link whose forms read an address typed without a scheme as https://."""

    def formfield(self, **kwargs: Any) -> Any:
        return super().formfield(**{"assume_scheme": "https", **kwargs})

    def deconstruct(self) -> Any:
        """Migrations see a plain URLField: the scheme matters to forms only."""
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.URLField", args, kwargs


class Release(models.Model):
    """Last successful GitHub release response, shown when GitHub cannot be reached."""

    repository = models.CharField(_("repository"), max_length=200, unique=True)
    payload = models.JSONField(_("GitHub response"))
    fetched_at = models.DateTimeField(_("fetched at"), auto_now=True)

    class Meta:
        verbose_name = _("release")
        verbose_name_plural = _("releases")

    def __str__(self) -> str:
        return f"{self.repository} {self.payload.get('tag_name', '')}"


class FaqArticleQuerySet(models.QuerySet["FaqArticle"]):
    def published(self) -> "FaqArticleQuerySet":
        return self.filter(is_published=True)

    def get_by_natural_key(self, slug: str) -> "FaqArticle":
        return self.get(slug=slug)


class FaqArticle(models.Model):
    """Help article in Markdown; a language left empty shows the other one."""

    slug = models.SlugField(_("address"), unique=True)
    title_ru = models.CharField(_("title (Russian)"), max_length=200, blank=True)
    body_ru = models.TextField(_("text (Russian)"), blank=True, help_text=_("Markdown."))
    title_en = models.CharField(_("title (English)"), max_length=200, blank=True)
    body_en = models.TextField(_("text (English)"), blank=True, help_text=_("Markdown."))
    position = models.PositiveIntegerField(_("position"), default=0)
    is_published = models.BooleanField(_("published"), default=True)
    credit = models.CharField(_("suggested by"), max_length=150, blank=True, help_text=_CREDIT_HELP)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = FaqArticleQuerySet.as_manager()

    class Meta:
        ordering = ("position", "id")
        verbose_name = _("FAQ article")
        verbose_name_plural = _("FAQ articles")

    def __str__(self) -> str:
        return self.title_in("ru")

    def natural_key(self) -> tuple[str]:
        """Fixtures match articles by slug, so reloading them updates and does not duplicate."""
        return (self.slug,)

    def clean(self) -> None:
        if not any(title and body for title, body in self._texts().values()):
            raise ValidationError(_("Fill in the title and text in at least one language."))

    def title_in(self, language: str) -> str:
        return self._text_in(language)[0]

    def body_in(self, language: str) -> str:
        return self._text_in(language)[1]

    def _texts(self) -> dict[str, tuple[str, str]]:
        return {"ru": (self.title_ru, self.body_ru), "en": (self.title_en, self.body_en)}

    def _text_in(self, language: str) -> tuple[str, str]:
        texts = self._texts()
        wanted = texts.get(language.split("-", maxsplit=1)[0], texts["ru"])
        if wanted[0] and wanted[1]:
            return wanted
        return next((text for text in texts.values() if text[0] and text[1]), wanted)


class MaterialTopic(models.TextChoices):
    INSTALL = "install", _("Installation")
    SPEED = "speed", _("Speed")
    BLOCKING = "blocking", _("Blocking")
    SECURITY = "security", _("Security")
    DEVICES = "devices", _("Devices")


class MaterialLanguage(models.TextChoices):
    RU = "ru", _("Russian")
    EN = "en", _("English")


class UsefulMaterialQuerySet(models.QuerySet["UsefulMaterial"]):
    def published(self) -> "UsefulMaterialQuerySet":
        return self.filter(is_published=True)

    def visible(self) -> "UsefulMaterialQuerySet":
        """What the public page shows: published and not marked broken by the last check."""
        return self.published().filter(is_broken=False)

    def broken(self) -> "UsefulMaterialQuerySet":
        """Published, but hidden from the public page because the last check found the link dead."""
        return self.published().filter(is_broken=True)

    def get_by_natural_key(self, slug: str) -> "UsefulMaterial":
        return self.get(slug=slug)


class UsefulMaterial(models.Model):
    """Link to an outside article with our own description; the article text is not copied."""

    slug = models.SlugField(_("address"), unique=True)
    title = models.CharField(_("title"), max_length=300)
    url = LinkField(_("link"), max_length=500, validators=[http_url])
    language = models.CharField(_("language"), max_length=2, choices=MaterialLanguage.choices)
    source = models.CharField(_("source"), max_length=100)
    topic = models.CharField(_("topic"), max_length=20, choices=MaterialTopic.choices)
    description_ru = models.TextField(_("description (Russian)"), blank=True)
    description_en = models.TextField(_("description (English)"), blank=True)
    position = models.PositiveIntegerField(_("position"), default=0)
    is_published = models.BooleanField(pgettext_lazy("material", "published"), default=True)
    checked_at = models.DateField(_("link checked"), null=True, blank=True)
    is_broken = models.BooleanField(_("broken link"), default=False)
    check_error = models.CharField(_("check error"), max_length=200, blank=True)
    credit = models.CharField(_("suggested by"), max_length=150, blank=True, help_text=_CREDIT_HELP)

    objects = UsefulMaterialQuerySet.as_manager()

    class Meta:
        ordering = ("position", "id")
        verbose_name = _("useful material")
        verbose_name_plural = _("useful materials")

    def __str__(self) -> str:
        return self.title

    def natural_key(self) -> tuple[str]:
        """Fixtures match materials by slug, so reloading them updates and does not duplicate."""
        return (self.slug,)

    def clean(self) -> None:
        if not (self.description_ru or self.description_en):
            raise ValidationError(_("Describe the material in at least one language."))

    def description_in(self, language: str) -> str:
        """The description in the language asked for, else the one that is filled in."""
        if language.split("-", maxsplit=1)[0] == MaterialLanguage.EN:
            return self.description_en or self.description_ru
        return self.description_ru or self.description_en


class SuggestionKind(models.TextChoices):
    LINK = "link", _("Link to an article")
    TEXT = "text", _("My own text")


class SuggestionStatus(models.TextChoices):
    PENDING = "pending", _("Waiting for review")
    ACCEPTED = "accepted", _("Published")
    REJECTED = "rejected", _("Declined")


class SuggestionQuerySet(models.QuerySet["Suggestion"]):
    def pending(self) -> "SuggestionQuerySet":
        return self.filter(status=SuggestionStatus.PENDING)


class Suggestion(models.Model):
    """A user's link or text for the guides; the staff publish it as a material or FAQ article."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("author"),
        on_delete=models.CASCADE,
        related_name="guide_suggestions",
    )
    kind = models.CharField(_("what"), max_length=4, choices=SuggestionKind.choices)
    title = models.CharField(_("title"), max_length=300)
    url = LinkField(_("link"), max_length=500, blank=True, validators=[http_url])
    body = models.TextField(_("text"), blank=True, max_length=20000, help_text=_("Markdown."))
    language = models.CharField(_("language"), max_length=2, choices=MaterialLanguage.choices)
    topic = models.CharField(_("topic"), max_length=20, choices=MaterialTopic.choices)
    comment = models.TextField(
        _("comment"), blank=True, max_length=2000, help_text=_("Only the staff see it.")
    )
    credit_author = models.BooleanField(
        _("name me as the author"),
        default=False,
        help_text=_("Your name is shown with the published text."),
    )
    status = models.CharField(
        _("status"),
        max_length=8,
        choices=SuggestionStatus.choices,
        default=SuggestionStatus.PENDING,
    )
    reject_reason = models.TextField(_("reason"), blank=True)
    material = models.ForeignKey(
        UsefulMaterial,
        verbose_name=_("useful material"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    article = models.ForeignKey(
        FaqArticle,
        verbose_name=_("FAQ article"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("reviewed by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("sent at"), auto_now_add=True)

    objects = SuggestionQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("suggestion")
        verbose_name_plural = _("suggestions")

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        if self.kind == SuggestionKind.LINK and not self.url:
            raise ValidationError({"url": _("Give the link to the article.")})
        if self.kind == SuggestionKind.TEXT and not self.body.strip():
            raise ValidationError({"body": _("Write the text.")})

    @property
    def author_name(self) -> str:
        """How the author is named when they asked for it."""
        return self.author.get_full_name() or self.author.get_username()


class BuiltinEntryQuerySet(models.QuerySet["BuiltinEntry"]):
    def deleted(self) -> "BuiltinEntryQuerySet":
        """Entries offered once whose article or material the panel no longer has."""
        missing = models.Q(pk__in=[])
        for model in (FaqArticle, UsefulMaterial):
            kept = model._default_manager.filter(slug=models.OuterRef("slug"))
            missing |= models.Q(models.Q(model=model._meta.label_lower), ~models.Exists(kept))
        return self.filter(missing)


class BuiltinEntry(models.Model):
    """A built-in FAQ article or material once offered to this panel; it is never offered again.

    A deleted one stays listed in the admin and can be restored from the built-in fixture.
    """

    model = models.CharField(_("model"), max_length=100)
    slug = models.SlugField(_("address"))
    offered_at = models.DateTimeField(_("offered at"), auto_now_add=True)

    objects = BuiltinEntryQuerySet.as_manager()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("model", "slug"), name="guides_builtin_entry_unique"),
        )
        verbose_name = _("built-in entry")
        verbose_name_plural = _("built-in entries")

    def __str__(self) -> str:
        return f"{self.model} {self.slug}"
