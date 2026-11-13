from __future__ import annotations

from typing import Any, ClassVar

from django.apps import apps
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import QuerySet
from django.forms import Field
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.urls import URLPattern, path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.guides.models import BuiltinEntry, FaqArticle, UsefulMaterial
from apps.guides.services.builtin import BuiltinContent
from apps.guides.services.markdown import render_markdown

_MARKDOWN_FIELDS = ("body_ru", "body_en")


@admin.register(FaqArticle)
class FaqArticleAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """The texts get a preview button: the panel renders them as the public page does."""

    list_display = ("__str__", "title_en", "slug", "position", "is_published", "updated_at")
    list_editable = ("position", "is_published")
    list_filter = ("is_published",)
    search_fields = ("title_ru", "title_en", "body_ru", "body_en", "slug")
    prepopulated_fields: ClassVar = {"slug": ("title_en",)}
    fieldsets = (
        (None, {"fields": ("slug", "position", "is_published", "credit")}),
        (_("Russian"), {"fields": ("title_ru", "body_ru")}),
        (_("English"), {"fields": ("title_en", "body_en")}),
    )

    class Media:
        js = ("js/markdown-preview.js",)

    def get_urls(self) -> list[URLPattern]:
        preview = path(
            "preview/",
            self.admin_site.admin_view(self.preview_view),
            name="guides_faqarticle_preview",
        )
        return [preview, *super().get_urls()]

    def formfield_for_dbfield(
        self, db_field: models.Field[Any, Any], request: HttpRequest, **kwargs: Any
    ) -> Field | None:
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if field is not None and db_field.name in _MARKDOWN_FIELDS:
            field.widget.attrs.update(
                {
                    "data-markdown-preview": reverse("admin:guides_faqarticle_preview"),
                    "data-preview-label": _("Preview"),
                    "data-preview-error": _("The preview failed. Save the article to see it."),
                }
            )
        return field

    def preview_view(self, request: HttpRequest) -> HttpResponse:
        """The posted Markdown as safe HTML; nothing is saved."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (self.has_add_permission(request) or self.has_change_permission(request)):
            raise PermissionDenied
        return HttpResponse(render_markdown(request.POST.get("text", "")))


@admin.register(UsefulMaterial)
class UsefulMaterialAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Broken links stay here with the error; the public page hides them."""

    list_display = (
        "title",
        "topic",
        "language",
        "source",
        "position",
        "is_published",
        "is_broken",
        "checked_at",
        "check_error",
    )
    list_editable = ("position", "is_published")
    list_filter = ("is_broken", "topic", "language", "is_published")
    search_fields = ("title", "url", "source", "description_ru", "description_en", "slug")
    prepopulated_fields: ClassVar = {"slug": ("title",)}
    readonly_fields = ("checked_at", "is_broken", "check_error")
    fieldsets = (
        (None, {"fields": ("slug", "title", "url", "language", "source", "topic")}),
        (None, {"fields": ("position", "is_published", "credit")}),
        (_("Our description"), {"fields": ("description_ru", "description_en")}),
        (_("Link check"), {"fields": ("checked_at", "is_broken", "check_error")}),
    )


@admin.register(BuiltinEntry)
class DeletedBuiltinEntryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """The journal of deleted built-in FAQ articles and materials, with an action to restore them.

    A deleted built-in entry never comes back on its own (services/builtin.py); this is the way.
    """

    list_display = ("slug", "kind", "offered_at")
    list_filter = ("model",)
    search_fields = ("slug",)
    actions = ("restore",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[BuiltinEntry]:
        return BuiltinEntry.objects.deleted()

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        title = _("Deleted built-in FAQ articles and materials")
        return super().changelist_view(request, {"title": title, **(extra_context or {})})

    @admin.display(description=_("kind"))
    def kind(self, entry: BuiltinEntry) -> str:
        return str(apps.get_model(entry.model)._meta.verbose_name)

    @admin.action(description=_("Restore the selected entries"), permissions=["restore"])
    def restore(self, request: HttpRequest, queryset: QuerySet[BuiltinEntry]) -> None:
        restored = BuiltinContent().restore(queryset)
        text = ngettext(
            "%(count)d entry is restored.", "%(count)d entries are restored.", restored
        ) % {"count": restored}
        self.message_user(request, text, messages.SUCCESS if restored else messages.WARNING)

    def has_restore_permission(self, request: HttpRequest) -> bool:
        return all(
            request.user.has_perm(f"guides.add_{model._meta.model_name}")
            for model in (FaqArticle, UsefulMaterial)
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
