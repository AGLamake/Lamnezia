import re
from typing import TYPE_CHECKING, Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.models import SiteSettings

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

# Secrets are form-only fields: the stored value is never sent back to the browser.
SECRET_FIELDS: dict[str, tuple["StrOrPromise", "StrOrPromise"]] = {
    "smtp_password": (_("SMTP password"), ""),
    "telegram_bot_token": (_("Telegram bot token"), _("From @BotFather.")),
    "discord_client_secret": (
        _("Discord client secret"),
        _("OAuth2 page of the Discord application."),
    ),
    "discord_bot_token": (
        _("Discord bot token"),
        _("Bot page of the Discord application. The bot sends keys and notifications."),
    ),
    "github_token": (
        _("GitHub token"),
        _("Without scopes. Raises the GitHub limit for Amnezia app download links."),
    ),
}

TELEGRAM_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


class SiteSettingsForm(forms.ModelForm):  # type: ignore[type-arg]
    """Panel settings. An empty secret keeps the saved one; the checkbox next to it removes it."""

    class Meta:
        model = SiteSettings
        fields = (
            "site_name",
            "registration_mode",
            "default_plan",
            "require_email_confirmation",
            "key_reveal_policy",
            "bot_key_delivery",
            "guide_suggestions",
            "smtp_host",
            "smtp_port",
            "smtp_security",
            "smtp_username",
            "from_email",
            "telegram_bot_username",
            "telegram_login",
            "discord_client_id",
            "discord_login",
            "webhooks_private_networks",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The /setdomain hint with the panel's domain stands under this field on the page.
        self.fields["telegram_login"].help_text = _(
            "Users with a linked Telegram can sign in with it."
        )
        for name, (label, help_text) in SECRET_FIELDS.items():
            saved = bool(getattr(self.instance, name))
            self.fields[name] = forms.CharField(
                label=label,
                required=False,
                strip=True,
                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                help_text=_("Saved. Leave empty to keep it.") if saved else help_text,
            )
            if saved:
                self.fields[f"clear_{name}"] = forms.BooleanField(
                    label=_("Remove the saved value"), required=False
                )

    def clean_telegram_bot_username(self) -> str:
        """The bare name t.me links need: "@name" or a pasted t.me link are taken too."""
        name: str = self.cleaned_data["telegram_bot_username"].strip()
        name = name.removeprefix("https://").removeprefix("t.me/").lstrip("@")
        if name and not TELEGRAM_USERNAME.fullmatch(name):
            raise forms.ValidationError(_("A bot username: 5 to 32 Latin letters, digits and _."))
        return name

    def save(self, commit: bool = True) -> SiteSettings:
        site: SiteSettings = super().save(commit=False)
        for name in SECRET_FIELDS:
            if self.cleaned_data.get(f"clear_{name}"):
                setattr(site, name, "")
            elif self.cleaned_data[name]:
                setattr(site, name, self.cleaned_data[name])
        if commit:
            site.save()
        return site
