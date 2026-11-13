from typing import Any

from django.contrib.messages.views import SuccessMessageMixin
from django.forms import ModelForm
from django.http import HttpResponse
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import UpdateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.audit.services import journal as audit
from apps.core.forms import SiteSettingsForm
from apps.core.models import SiteSettings


class SiteSettingsView(AdminRequiredMixin, SuccessMessageMixin, UpdateView):  # type: ignore[type-arg]
    form_class = SiteSettingsForm
    template_name = "core/settings.html"
    success_url = reverse_lazy("core:settings")
    success_message = _("Settings saved.")

    def get_object(self, queryset: object = None) -> SiteSettings:
        # A fresh row, not the cached one: a form with errors must not change the cache.
        return SiteSettings.load()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        callback = self.request.build_absolute_uri(reverse("accounts:discord-callback"))
        # The domain @BotFather's /setdomain takes: the host without a port.
        domain = self.request.get_host().rsplit(":", 1)[0]
        return super().get_context_data(discord_redirect=callback, telegram_domain=domain, **kwargs)

    def form_valid(self, form: "ModelForm[SiteSettings]") -> HttpResponse:
        response = super().form_valid(form)
        # Field names only: the values may be secrets.
        audit.log_request(
            self.request, "core.settings_change", self.object, fields=sorted(form.changed_data)
        )
        return response
