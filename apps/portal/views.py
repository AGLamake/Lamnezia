import base64
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView, TemplateView
from django.views.generic.base import TemplateResponseMixin

from amnezia_core.xray import XRAY_CONTAINER
from apps.accounts.models import User
from apps.accounts.services.link_offer import LinkOffer
from apps.audit.services.journal import client_ip
from apps.core.services.site_settings import cached_settings
from apps.dashboard.services.chart import period_links, traffic_chart
from apps.guides.services.devices import DetectedDevice, Device
from apps.keys.services.actions import KeyRevokingError, OwnerCannotHaveKeysError
from apps.keys.services.instances import instances
from apps.keys.services.subscription import MySubscription
from apps.keys.services.xray_profiles import request_profiles
from apps.portal.forms import GetKeyForm, RenameKeyForm
from apps.portal.services.keys import (
    InstanceNotAllowedError,
    KeyLimitReachedError,
    MyKeys,
)
from apps.portal.services.reachability import Probes
from apps.portal.services.servers import my_servers
from apps.portal.services.traffic import MyTraffic
from apps.portal.services.usage import Usage
from config.security import policy_allowing


class _PortalView(LoginRequiredMixin):
    """A page of the signed-in user's own access."""

    request: HttpRequest

    @property
    def user(self) -> User:
        assert isinstance(self.request.user, User)
        return self.request.user

    @property
    def my_keys(self) -> MyKeys:
        if not hasattr(self, "_my_keys"):
            self._my_keys = MyKeys(self.user, client_ip(self.request))
        return self._my_keys

    def device(self) -> Device | None:
        """The device the user chose when getting the key, or the one they browse from."""
        chosen = self.request.GET.get("device", "")
        if chosen in Device.values:
            return Device(chosen)
        return DetectedDevice.from_user_agent(self.request.headers.get("User-Agent", "")).device


class _ProbingView(TemplateResponseMixin):
    """A page with `probes` in its context lets the browser reach their TCP addresses."""

    def render_to_response(self, context: dict[str, Any], **kwargs: Any) -> HttpResponse:
        response = super().render_to_response(context, **kwargs)
        probes: Probes = context.get("probes", Probes())
        if origins := probes.origins:
            response.headers["Content-Security-Policy"] = policy_allowing(origins)
        return response


class _KeyView(_PortalView):
    """A page about one key of the user; a user waiting for approval has none to open."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if request.method == "POST" and self.my_keys.revoking(kwargs["pk"]):
            return redirect("portal:index")
        self.key = get_object_or_404(self.my_keys.all(), pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def to_key(self) -> HttpResponse:
        return redirect("portal:key", pk=self.key.pk)


class PortalHomeView(_PortalView, _ProbingView, TemplateView):
    template_name = "portal/index.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        my_keys = self.my_keys
        context = super().get_context_data(**kwargs)
        context.update(LinkOffer(self.user, cached_settings.get()).context())
        if not self.user.can_have_keys or not my_keys.limits.has_plan:
            return context
        titles = instances.titles()
        keyed = my_keys.keyed_instances()
        return {
            **context,
            "keys": [(key, titles.get(key.source, "")) for key in my_keys.all()],
            "limits": my_keys.limits,
            "can_get_key": my_keys.can_get_key(),
            "usage": Usage.of(self.user, my_keys.limits, timezone.now()),
            "probes": Probes.of(keyed),
            "offers_subscription": MySubscription(self.user).available
            and any(instance.container.name == XRAY_CONTAINER for instance in keyed),
        }


class UsageView(_PortalView, TemplateView):
    """The user's traffic by key over a day, week or month, and how long the limit lasts."""

    template_name = "portal/usage.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.user.is_authenticated and not (
            self.user.can_have_keys and self.my_keys.limits.has_plan
        ):
            return redirect("portal:index")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        now = timezone.now()
        query = self.request.GET
        traffic = MyTraffic.of(self.user, query.get("period", ""), query.get("key", ""), now)
        titles = instances.titles()
        return super().get_context_data(
            traffic=traffic,
            rows=[(row, titles.get(row.key.source, "")) for row in traffic.keys],
            chart=traffic_chart(traffic.series, traffic.window),
            periods=period_links(query, traffic.period),
            usage=Usage.of(self.user, self.my_keys.limits, now),
            forecast=traffic.forecast(),
            **kwargs,
        )


class SubscriptionView(_PortalView, TemplateView):
    """The user's subscription link for XRay clients: shown, replaced with a new one, deleted."""

    template_name = "portal/subscription.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.subscription = MySubscription(self.user)
        if not self.subscription.available:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        token = self.subscription.token()
        url = self.request.build_absolute_uri(reverse("subscription-feed", args=[token]))
        return super().get_context_data(url=url if token else None, **kwargs)

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.POST.get("action") == "delete":
            self.subscription.delete()
            messages.success(request, _("The subscription link is deleted."))
        else:
            self.subscription.renew()
            messages.success(request, _("A new subscription link is ready."))
        return redirect("portal:subscription")


class ServerStatusView(_PortalView, TemplateView):
    template_name = "portal/servers.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        servers = my_servers(self.my_keys.limits) if self.user.can_have_keys else []
        return super().get_context_data(servers=servers, **kwargs)


class GetKeyView(_PortalView, _ProbingView, FormView):  # type: ignore[type-arg]
    template_name = "portal/get_key.html"
    form_class = GetKeyForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.user.is_authenticated and not self.my_keys.can_get_key():
            messages.error(request, _("You cannot get another key now."))
            return redirect("portal:index")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "instances": self.my_keys.instances()}

    def get_initial(self) -> dict[str, Any]:
        return {"device": self.device()}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        # Only servers the user has keys on: the others' addresses stay hidden.
        probes = Probes.of(self.my_keys.keyed_instances())
        return super().get_context_data(probes=probes, **kwargs)

    def form_valid(self, form: GetKeyForm) -> HttpResponse:
        try:
            key = self.my_keys.issue(form.cleaned_data["source"], form.key_name())
        except (KeyLimitReachedError, OwnerCannotHaveKeysError):
            messages.error(self.request, _("You cannot get another key now."))
            return redirect("portal:index")
        except InstanceNotAllowedError:
            form.add_error("source", _("This server is not available to you."))
            return self.form_invalid(form)
        url = reverse("portal:key", args=[key.pk])
        return redirect(f"{url}?device={form.cleaned_data['device']}")


class KeyView(_KeyView, _ProbingView, TemplateView):
    """The key, its actions, and the way to get it into the app once it is ready."""

    template_name = "portal/key.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        instance = next((i for i in instances if i.source == self.key.source), None)
        return super().get_context_data(
            title=instance.title if instance else "",
            server_id=instance.server_id if instance else None,
            rename_form=RenameKeyForm(initial={"name": self.key.name}),
            probes=Probes.of(self.my_keys.keyed_instances(self.key)),
            **self.state_context(),
            **kwargs,
        )

    def state_context(self) -> dict[str, Any]:
        """Context of `portal/_key_state.html`, which polls itself while the key is issued or
        its profile is built (an imported XRay key).
        """
        return {
            "key": self.key,
            "profile_pending": request_profiles([self.key]),
            "device": self.device(),
            "can_reveal": self.my_keys.can_reveal(self.key),
            "reveal_once": self.my_keys.reveals_once,
            "issue_failed": self.my_keys.issue_failed(self.key),
            "poll_seconds": settings.PROGRESS_POLL_SECONDS,
        }


class KeyStateView(KeyView):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return render(request, "portal/_key_state.html", self.state_context())


class KeyRevealView(_KeyView, View):
    """POST, so that link previews and prefetching never use up a key shown only once."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        export = self.my_keys.reveal(self.key)
        if export is None:
            messages.error(
                request, _("This key cannot be shown again. Re-issue it to get a new one.")
            )
            return self.to_key()
        files = [
            {
                "label": file.label,
                "extension": file.extension,
                "data": f"data:{file.content_type};charset=utf-8;base64,"
                f"{base64.b64encode(file.content.encode()).decode()}",
            }
            for file in export.files
        ]
        return render(
            request,
            "portal/reveal.html",
            {
                "key": self.key,
                "device": self.device(),
                "vpn_link": export.vpn_link,
                "vless_link": export.vless_link,
                "qr_codes": export.qr_codes(),
                "files": files,
            },
        )


class KeyRenameView(_KeyView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        form = RenameKeyForm(request.POST)
        if form.is_valid():
            self.my_keys.rename(self.key, form.cleaned_data["name"])
            messages.success(request, _("The key will be renamed in a moment."))
        else:
            messages.error(request, _("Enter a name."))
        return self.to_key()


class KeyReissueView(_KeyView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        try:
            key = self.my_keys.reissue(self.key)
        except KeyRevokingError:
            return redirect("portal:index")
        except (InstanceNotAllowedError, OwnerCannotHaveKeysError):
            messages.error(request, _("This key cannot be re-issued. Delete it and get a new one."))
            return self.to_key()
        messages.success(
            request, _("The old key is revoked. The new one will be ready in a moment.")
        )
        return redirect("portal:key", pk=key.pk)


class KeyDeleteView(_KeyView, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        try:
            self.my_keys.delete(self.key)
        except KeyRevokingError:
            return redirect("portal:index")
        messages.success(request, _("The key is deleted and stops working in a moment."))
        return redirect("portal:index")
