"""Channel forms: one per kind, each names `target` and `secret` in its own terms."""

from typing import TYPE_CHECKING, Any, ClassVar

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.utils.translation import gettext_lazy as _

from apps.notifications.events import routable_events
from apps.notifications.models import Channel, ChannelKind, DeliveryStatus
from apps.notifications.services.addresses import AddressGuard, BlockedAddressError
from apps.notifications.services.channels import parse_addresses
from apps.notifications.services.delivery import set_routes

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

_http_url = URLValidator(schemes=["http", "https"])


class ChannelForm(forms.ModelForm):  # type: ignore[type-arg]
    """The secret is never sent back to the browser: empty on edit keeps the saved one."""

    kind: ClassVar[ChannelKind]
    secret_label: ClassVar["StrOrPromise | None"] = None
    secret_help: ClassVar["StrOrPromise"] = ""
    secret_required: ClassVar[bool] = True

    events = forms.MultipleChoiceField(
        label=_("Events"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=routable_events,
    )

    class Meta:
        model = Channel
        fields: tuple[str, ...] = ("name", "target", "enabled")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.instance.kind = self.kind
        if self.instance.pk:
            self.initial["events"] = list(self.instance.routes.values_list("event", flat=True))
        if self.secret_label is not None:
            self.fields["secret"] = self._secret_field()

    def _secret_field(self) -> forms.CharField:
        saved = bool(self.instance.secret)
        return forms.CharField(
            label=self.secret_label,
            required=self.secret_required and not saved,
            strip=True,
            widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
            help_text=_("Saved. Leave empty to keep it.") if saved else self.secret_help,
        )

    def save(self, commit: bool = True) -> Channel:
        channel: Channel = super().save(commit=False)
        if self.cleaned_data.get("secret"):
            channel.secret = self.cleaned_data["secret"]
        channel.save()
        set_routes(channel, self.cleaned_data["events"])
        return channel


class EmailChannelForm(ChannelForm):
    kind = ChannelKind.EMAIL

    class Meta(ChannelForm.Meta):
        labels: ClassVar = {"target": _("Addresses")}
        help_texts: ClassVar = {"target": _("One or more, separated by commas.")}

    def clean_target(self) -> str:
        addresses = parse_addresses(self.cleaned_data["target"])
        if not addresses:
            raise ValidationError(_("Enter at least one address."))
        for address in addresses:
            validate_email(address)
        return ", ".join(addresses)


class TelegramChannelForm(ChannelForm):
    kind = ChannelKind.TELEGRAM
    secret_label = _("Bot token")
    secret_help = _("Empty: the bot from the panel settings. The bot must be in the chat.")
    secret_required = False

    class Meta(ChannelForm.Meta):
        labels: ClassVar = {"target": _("Chat ID")}
        help_texts: ClassVar = {"target": _("A number, e.g. -1001234567890, or @channel_name.")}

    def clean_target(self) -> str:
        chat_id: str = self.cleaned_data["target"].strip()
        if not chat_id:
            raise ValidationError(_("Enter the chat ID."))
        return chat_id


class DiscordChannelForm(ChannelForm):
    kind = ChannelKind.DISCORD
    secret_label = _("Webhook URL")
    secret_help = _("Channel settings → Integrations → Webhooks → Copy Webhook URL.")

    class Meta(ChannelForm.Meta):
        fields = ("name", "enabled")

    def clean_secret(self) -> str:
        url: str = self.cleaned_data["secret"]
        if url:
            _http_url(url)
            _public_url(url)
        return url


class DiscordDmChannelForm(ChannelForm):
    """Sent by the Discord bot from the panel settings."""

    kind = ChannelKind.DISCORD_DM

    class Meta(ChannelForm.Meta):
        labels: ClassVar = {"target": _("Discord user ID")}
        help_texts: ClassVar = {
            "target": _(
                "Developer mode in Discord, then right click the user and Copy User ID. "
                "The user must share a server with the panel bot."
            )
        }

    def clean_target(self) -> str:
        user_id: str = self.cleaned_data["target"].strip()
        if not user_id.isdigit():
            raise ValidationError(_("The Discord user ID is a number."))
        return user_id


class WebhookChannelForm(ChannelForm):
    kind = ChannelKind.WEBHOOK
    secret_label = _("Signing secret")
    secret_help = _("The body is signed with it in the X-Lamnezia-Signature header.")

    class Meta(ChannelForm.Meta):
        labels: ClassVar = {"target": _("URL")}

    def clean_target(self) -> str:
        url: str = self.cleaned_data["target"].strip()
        _http_url(url)
        _public_url(url)
        return url


def _public_url(url: str) -> None:
    try:
        AddressGuard.from_settings().check(url)
    except BlockedAddressError as error:
        raise ValidationError(
            _(
                "%(address)s is an address in a private network. Allow such addresses in the "
                "panel settings if the receiver is in your own network."
            ),
            params={"address": error.address},
        ) from None


CHANNEL_FORMS: dict[str, type[ChannelForm]] = {
    form.kind: form
    for form in (
        EmailChannelForm,
        TelegramChannelForm,
        DiscordChannelForm,
        DiscordDmChannelForm,
        WebhookChannelForm,
    )
}


class DeliveryFilterForm(forms.Form):
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("Any")), *DeliveryStatus.choices],
    )
