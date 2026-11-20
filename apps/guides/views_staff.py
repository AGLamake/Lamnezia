"""FAQ articles and useful materials in the panel: lists, editing, publication, order, deletion,
the Markdown preview and the deleted built-in entries. For administrators.
"""

from typing import Any

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.guides.forms import EntryForm, entry_form
from apps.guides.models import BuiltinEntry
from apps.guides.services.editor import SHELVES, Entry, GuideEditor, Shelf
from apps.guides.services.markdown import render_markdown


class _StaffView(AdminRequiredMixin):
    def editor(self) -> GuideEditor:
        return GuideEditor(self.member, ip=self.client_ip)


class _ShelfView(_StaffView):
    kwargs: dict[str, Any]

    @property
    def shelf(self) -> Shelf:
        shelf = SHELVES.get(self.kwargs["shelf"])
        if shelf is None:
            raise Http404
        return shelf

    def entry(self, pk: int) -> Entry:
        return get_object_or_404(self.shelf.entries(), pk=pk)

    def back(self) -> HttpResponse:
        return redirect("guides:staff", shelf=self.shelf.slug)


class EntryListView(_ShelfView, TemplateView):
    template_name = "guides/staff/list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        shelf = self.shelf
        return super().get_context_data(
            **kwargs | {"shelf": shelf, "entries": shelf.entries(), **_tabs_context()}
        )


class EntryFormView(_ShelfView, TemplateView):
    """A new entry without `pk`, the entry to edit with it."""

    template_name = "guides/staff/form.html"

    def get(self, request: HttpRequest, shelf: str, pk: int | None = None) -> HttpResponse:
        return self.page(self.form(pk))

    def post(self, request: HttpRequest, shelf: str, pk: int | None = None) -> HttpResponse:
        form = self.form(pk, request.POST)
        if not form.is_valid():
            return self.page(form)
        self.editor().save(form.save(commit=False))
        messages.success(request, _("Saved."))
        return self.back()

    def form(self, pk: int | None, data: Any = None) -> EntryForm:
        instance = self.entry(pk) if pk is not None else self.shelf.model()
        return entry_form(self.shelf.model)(data, instance=instance)

    def page(self, form: EntryForm) -> HttpResponse:
        entry = None if form.instance._state.adding else form.instance
        return self.render_to_response(
            self.get_context_data(shelf=self.shelf, form=form, entry=entry)
        )


class EntryPublishView(_ShelfView, View):
    def post(self, request: HttpRequest, shelf: str, pk: int) -> HttpResponse:
        self.editor().publish(self.entry(pk), request.POST.get("published") == "1")
        return self.back()


class EntryMoveView(_ShelfView, View):
    def post(self, request: HttpRequest, shelf: str, pk: int) -> HttpResponse:
        step = -1 if request.POST.get("direction") == "up" else 1
        self.editor().move(self.entry(pk), step)
        return self.back()


class EntryDeleteView(_ShelfView, View):
    def post(self, request: HttpRequest, shelf: str, pk: int) -> HttpResponse:
        self.editor().delete(self.entry(pk))
        messages.success(request, _("Deleted."))
        return self.back()


class DeletedBuiltinView(_StaffView, TemplateView):
    """Built-in entries the panel no longer has: they never come back on their own."""

    template_name = "guides/staff/deleted.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        titles = {shelf.model._meta.label_lower: shelf.title for shelf in SHELVES.values()}
        deleted = [
            (offered, titles.get(offered.model, offered.model))
            for offered in BuiltinEntry.objects.deleted().order_by("model", "slug")
        ]
        return super().get_context_data(deleted=deleted, **_tabs_context(), **kwargs)


class BuiltinRestoreView(_StaffView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        offered = get_object_or_404(BuiltinEntry.objects.deleted(), pk=pk)
        if self.editor().restore(offered):
            messages.success(request, _("Restored."))
        else:
            messages.warning(request, _("This version of the panel no longer ships this entry."))
        return redirect("guides:staff-deleted")


class MarkdownPreviewView(_StaffView, View):
    """The posted Markdown as the public page shows it, cleaned; nothing is saved."""

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(render_markdown(request.POST.get("text", "")))


def _tabs_context() -> dict[str, Any]:
    return {"shelves": SHELVES.values(), "deleted_count": BuiltinEntry.objects.deleted().count()}
