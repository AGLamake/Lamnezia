from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views import View
from django.views.generic import DetailView, FormView, ListView, TemplateView

from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import Level, Section
from apps.core.paging import PageSizeMixin
from apps.keys.forms import (
    QUOTA_REACHED,
    KeyAddressForm,
    KeyAssignForm,
    KeyBulkForm,
    KeyFilterForm,
    KeyIssueForm,
)
from apps.keys.models import AccessKey, DriftKind
from apps.keys.services.actions import KeyActions
from apps.keys.services.address import KeyAddress
from apps.keys.services.drift import ALLOWED_ACTIONS, DriftAction, DriftActions
from apps.keys.services.instances import instances
from apps.keys.services.key_copies import LACKED_KINDS, key_copies
from apps.keys.services.profile import KeyExport
from apps.keys.services.xray_profiles import request_profiles
from apps.keys.services.zone import KeyQuotaReachedError, KeyZone
from apps.operations.models import Operation


def _to_operation(operation: Operation) -> HttpResponse:
    return redirect("operations:detail", pk=operation.pk)


class _KeysStaffView(SectionRequiredMixin):
    """The services act on behalf of the signed-in staff member, from their address.

    Keys outside the member's zone are as good as missing.
    """

    section = Section.KEYS

    kwargs: dict[str, Any]

    @cached_property
    def key_zone(self) -> KeyZone:
        return KeyZone(self.member)

    def get_key(self) -> AccessKey:
        return get_object_or_404(self.key_zone.keys(), pk=self.kwargs["pk"])

    @property
    def actions(self) -> KeyActions:
        return KeyActions(self.member, self.client_ip)

    @property
    def drift_actions(self) -> DriftActions:
        return DriftActions(self.member, self.client_ip)


class KeyListView(PageSizeMixin, _KeysStaffView, ListView):  # type: ignore[type-arg]
    template_name = "keys/list.html"
    context_object_name = "keys"

    def get_queryset(self) -> Any:
        self.filter_form = KeyFilterForm(self.request.GET or None, zone=self.key_zone)
        keys = self.key_zone.keys().select_related("user").order_by("-created_at")
        return self.filter_form.filter(keys)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(
            filter_form=self.filter_form,
            bulk_form=KeyBulkForm(zone=self.key_zone),
            drift_count=self.key_zone.drifts().count(),
            **kwargs,
        )
        # "Server · protocol" instead of the internal instance id; a gone one keeps its id.
        titles = self.key_zone.titles()
        context["rows"] = [(key, titles.get(key.source, key.source)) for key in context["keys"]]
        return context


class KeyIssueView(_KeysStaffView, FormView):  # type: ignore[type-arg]
    template_name = "keys/issue.html"
    form_class = KeyIssueForm

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "zone": self.key_zone}

    def form_valid(self, form: KeyIssueForm) -> HttpResponse:
        data = form.cleaned_data
        try:
            operation = self.key_zone.issue(
                self.actions, data["source"], data["name"], data["user"]
            )
        except KeyQuotaReachedError:
            form.add_error(None, QUOTA_REACHED)
            return self.form_invalid(form)
        return _to_operation(operation)


class KeyDetailView(_KeysStaffView, DetailView):  # type: ignore[type-arg]
    template_name = "keys/detail.html"
    context_object_name = "key"

    def get_object(self, queryset: Any = None) -> AccessKey:
        return self.get_key()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        address = KeyAddress(self.object, self.member, self.client_ip)
        return super().get_context_data(
            assign_form=KeyAssignForm(zone=self.key_zone),
            address_form=KeyAddressForm(address=address) if address.choices else None,
            instance_title=instances.titles().get(self.object.source, self.object.source),
            profile_pending=request_profiles([self.object]),
            **kwargs,
        )


class KeyActionView(_KeysStaffView, View):
    """POST-only state changes; each one starts an operation and opens its progress page."""

    def post(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        key = self.get_key()
        operation = self.actions.change_state(key, action)
        if operation is None:
            raise Http404
        return _to_operation(operation)


class KeyBulkView(_KeysStaffView, View):
    """Disable, enable or revoke the ticked keys: one operation per key, then the operations list.

    Keys whose status does not allow the change are skipped and counted.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        form = KeyBulkForm(request.POST, zone=self.key_zone)
        if not form.is_valid():
            messages.error(request, _("Tick the keys and choose what to do with them."))
            return redirect("keys:list")
        actions, change = self.actions, form.cleaned_data["action"]
        started = [actions.change_state(key, change) for key in form.cleaned_data["keys"]]
        done = sum(operation is not None for operation in started)
        if done:
            messages.success(
                request,
                ngettext("%(count)d operation started.", "%(count)d operations started.", done)
                % {"count": done},
            )
        if skipped := len(started) - done:
            messages.warning(
                request,
                ngettext(
                    "%(count)d key was skipped: its status does not allow it.",
                    "%(count)d keys were skipped: their status does not allow it.",
                    skipped,
                )
                % {"count": skipped},
            )
        return redirect("operations:list") if done else redirect("keys:list")


class KeyAssignView(_KeysStaffView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        key = self.get_key()
        form = KeyAssignForm(request.POST, zone=self.key_zone)
        if not form.is_valid() or key.is_gone:
            messages.error(request, _("Choose a user to give the key to."))
            return redirect("keys:detail", pk=key.pk)
        return _to_operation(self.actions.assign(key, form.cleaned_data["user"]))


class KeyAddressView(_KeysStaffView, View):
    """Send the key to another address of its server; the owner is told to import it again."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        address = KeyAddress(self.get_key(), self.member, self.client_ip)
        form = KeyAddressForm(request.POST, address=address)
        if not form.is_valid():
            messages.error(request, _("Choose one of the server addresses."))
        elif address.change(form.cleaned_data["host"]):
            messages.success(
                request, _("The key now sends clients to %(host)s.") % {"host": address.current}
            )
        return redirect("keys:detail", pk=pk)


class KeyResetFirstViewView(_KeysStaffView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        key = self.get_key()
        self.actions.reset_first_view(key)
        messages.success(request, _("The owner can open the key again."))
        return redirect("keys:detail", pk=key.pk)


class _ExportView(_KeysStaffView, View):
    """Staff may look at any key of their zone; every look is written to the audit journal.

    The key itself is a secret, so it takes the right to manage keys, not just to view them.
    """

    read_level = Level.MANAGE

    @cached_property
    def key(self) -> AccessKey:
        # Looked up after the role check, so a guest cannot tell which keys exist.
        return self.get_key()

    def export(self) -> KeyExport:
        if not self.key.can_export:
            raise PermissionDenied
        self.actions.record_view(self.key)
        return KeyExport.of(self.key)


class KeyRevealView(_ExportView):
    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        export = self.export()
        return render(
            request,
            "keys/reveal.html",
            {
                "key": self.key,
                "vpn_link": export.vpn_link,
                "vless_link": export.vless_link,
                "files": export.files,
                "qr_codes": export.qr_codes(),
            },
        )


class KeyFileView(_ExportView):
    def get(self, request: HttpRequest, pk: int, extension: str) -> HttpResponse:
        file = self.export().file(extension)
        if file is None:  # e.g. an XRay key has no file
            raise Http404
        response = HttpResponse(file.content, content_type=f"{file.content_type}; charset=utf-8")
        name = f"lamnezia-{self.key.pk}.{file.extension}"
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response


# Button text and style of each way to resolve a difference.
_DRIFT_BUTTONS = {
    (DriftKind.UNKNOWN_PEER, DriftAction.ACCEPT): (gettext_lazy("Add to the panel"), "btn-primary"),
    (DriftKind.UNKNOWN_PEER, DriftAction.REMOVE): (
        gettext_lazy("Remove from the server"),
        "btn-danger",
    ),
    (DriftKind.MISSING, DriftAction.RESTORE): (
        gettext_lazy("Restore on the server"),
        "btn-primary",
    ),
    (DriftKind.MISSING, DriftAction.ACCEPT): (gettext_lazy("Mark as revoked"), "btn-secondary"),
    (DriftKind.UNEXPECTED, DriftAction.REMOVE): (
        gettext_lazy("Remove from the server"),
        "btn-primary",
    ),
    (DriftKind.UNEXPECTED, DriftAction.ACCEPT): (gettext_lazy("Mark as active"), "btn-secondary"),
}


class DriftListView(_KeysStaffView, TemplateView):
    """Differences found by the latest reconciliation, grouped by instance."""

    template_name = "keys/drift.html"
    # Every part of the page is an action on the servers.
    read_level = Level.MANAGE

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        drifts = self.key_zone.drifts().select_related("key")
        titles = self.key_zone.titles()
        copies = key_copies.by_source()
        groups = [
            {
                "source": source,
                "title": title,
                "copies": copies.get(source, []),
                "drifts": [
                    {
                        "drift": d,
                        "buttons": [
                            (action, *_DRIFT_BUTTONS[(DriftKind(d.kind), action)])
                            for action in ALLOWED_ACTIONS[d.kind]
                        ],
                        # The peer works on the server but not through its copies.
                        "lacked": source in copies and d.kind in LACKED_KINDS,
                    }
                    for d in drifts
                    if d.source == source
                ],
            }
            for source, title in sorted(titles.items(), key=lambda item: item[1])
        ]
        # Sources that match get one short line each instead of a card of their own.
        return super().get_context_data(
            differing=[group for group in groups if group["drifts"]],
            matching=[group for group in groups if not group["drifts"]],
            **kwargs,
        )


class DriftCheckView(_KeysStaffView, View):
    def post(self, request: HttpRequest) -> HttpResponse:
        source = request.POST.get("source", "")
        if source not in self.key_zone.titles():
            raise Http404
        return _to_operation(self.drift_actions.check(source))


class DriftResolveView(_KeysStaffView, View):
    def post(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        drift = get_object_or_404(self.key_zone.drifts(), pk=pk)
        if action not in ALLOWED_ACTIONS[drift.kind]:
            raise Http404
        return _to_operation(self.drift_actions.resolve(drift, DriftAction(action)))
