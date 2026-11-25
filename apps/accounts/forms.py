from typing import Any, ClassVar

from django import forms
from django.contrib.auth import forms as auth_forms
from django.contrib.auth.forms import BaseUserCreationForm
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Invite, NotifyVia, User
from apps.accounts.services.backup_codes import looks_like_backup_code, normalized
from apps.plans.models import Plan

_TOKEN_LENGTH = 6


class TokenForm(forms.Form):
    """A six-digit code from the authenticator app."""

    token = forms.CharField(
        label=_("Code from the app"),
        max_length=16,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}
        ),
    )

    def clean_token(self) -> str:
        token = "".join(str(self.cleaned_data["token"]).split())
        if not (token.isdigit() and len(token) == _TOKEN_LENGTH):
            raise forms.ValidationError(_("Enter the six digits shown in the app."))
        return token


class _EndsTemporaryPassword:
    """A password the user chose ends the temporary one from the installer."""

    user: User

    def save(self, commit: bool = True) -> User:
        self.user.must_change_password = False
        return super().save(commit)  # type: ignore[misc,no-any-return]


class PasswordChangeForm(_EndsTemporaryPassword, auth_forms.PasswordChangeForm):
    pass


class SetPasswordForm(_EndsTemporaryPassword, auth_forms.SetPasswordForm):  # type: ignore[type-arg]
    pass


class SignInCodeForm(TokenForm):
    """At the sign-in prompt a backup code is taken instead of the code from the app."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["token"]
        field.label = _("Code from the app or a backup code")
        field.widget.attrs["inputmode"] = "text"

    def clean_token(self) -> str:
        code = normalized(str(self.cleaned_data["token"]))
        if looks_like_backup_code(code):
            return code
        return super().clean_token()


class RegistrationForm(BaseUserCreationForm):  # type: ignore[type-arg]
    """Checks the account data; the account itself is made by the Registration service."""

    email = forms.EmailField(
        label=_("Email"), max_length=254, widget=forms.EmailInput(attrs={"autocomplete": "email"})
    )

    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self) -> str:
        email: str = self.cleaned_data["email"]
        # Sign-in links go by email, so one address belongs to one account.
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("An account with this email already exists."))
        return email


class MagicLinkForm(forms.Form):
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class InviteForm(forms.ModelForm):  # type: ignore[type-arg]
    """A new invite; its plan is one of `plans`, those the member may give."""

    def __init__(self, *args: Any, plans: "QuerySet[Plan]", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = plans  # type: ignore[attr-defined]

    class Meta:
        model = Invite
        fields = ("plan", "max_uses", "expires_at", "note")
        widgets: ClassVar = {
            "expires_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            )
        }
        help_texts: ClassVar = {"expires_at": _("Empty: no end date.")}


class ApproveForm(forms.Form):
    # Shown in every row of the waiting list without a visible label, so it names itself.
    plan = forms.ModelChoiceField(
        label=_("Plan"),
        queryset=Plan.objects.all(),
        widget=forms.Select(attrs={"aria-label": _("Plan")}),
    )


class NotifyViaForm(forms.ModelForm):  # type: ignore[type-arg]
    """Where the user gets notifications (email or a linked messenger), in what language and,
    for a portal user, whether of a sign-in from a new place: the staff always get that one.
    """

    class Meta:
        model = User
        fields = ("notify_via", "language", "sign_in_alerts")
        widgets: ClassVar = {"notify_via": forms.RadioSelect}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.is_staff_member:
            del self.fields["sign_in_alerts"]
        linked = set(self.instance.linked_accounts.values_list("messenger", flat=True))
        self.fields["notify_via"].choices = [  # type: ignore[attr-defined]
            (value, label)
            for value, label in NotifyVia.choices
            if value == NotifyVia.EMAIL or value in linked
        ]


class EmailChangeForm(forms.Form):
    """A new address for the account, confirmed with the current password."""

    email = forms.EmailField(
        label=_("New email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, user: User, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_email(self) -> str:
        email: str = self.cleaned_data["email"]
        if email.casefold() == self.user.email.casefold():
            raise forms.ValidationError(_("This is your email already."))
        if User.objects.filter(email__iexact=email).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError(_("An account with this email already exists."))
        return email

    def clean_password(self) -> str:
        password: str = self.cleaned_data["password"]
        if not self.user.check_password(password):
            raise forms.ValidationError(_("The password is wrong."))
        return password
