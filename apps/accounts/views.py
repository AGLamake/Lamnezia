from http import HTTPStatus
from typing import Any, cast

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render, resolve_url
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import format_lazy
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import i18n
from django.views.generic import FormView
from django_otp import login as otp_login
from django_otp.models import Device
from django_otp.plugins.otp_static.models import StaticDevice

from apps.accounts.forms import SignInCodeForm, TokenForm
from apps.accounts.middleware import session_verified
from apps.accounts.models import User
from apps.accounts.services.backup_codes import BackupCodes
from apps.accounts.services.messengers import telegram_login_url
from apps.accounts.services.two_factor import TwoFactor
from apps.accounts.views_profile import to_profile
from apps.audit.services import journal as audit
from apps.core.mail import mail_configured
from apps.core.services.site_settings import cached_settings


def home(request: HttpRequest) -> HttpResponse:
    """Start page: guests sign in, the team goes to the dashboard, clients to their portal."""
    user = request.user
    if not isinstance(user, User):
        return redirect("accounts:login")
    return redirect("dashboard:index" if user.is_staff_member else "portal:index")


def set_language(request: HttpRequest) -> HttpResponse:
    """Django's language switch that also makes the choice the language of the user's
    notifications, so the bots and letters speak as the panel does.
    """
    response = i18n.set_language(request)
    language = request.POST.get("language", "")
    user = request.user
    if (
        request.method == "POST"
        and isinstance(user, User)
        and language in dict(settings.LANGUAGES)
        and user.language != language
    ):
        user.language = language
        user.save(update_fields=["language"])
    return response


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        site = cached_settings.get()
        telegram = site.telegram_login and site.telegram_linking
        return super().get_context_data(
            registration_open=site.registration_open,
            magic_link=mail_configured(),
            telegram_login=self._telegram_url(site.telegram_bot_token) if telegram else "",
            discord_login=site.discord_login and site.discord_oauth,
            **kwargs,
        )

    def _telegram_url(self, bot_token: str) -> str:
        origin = self.request.build_absolute_uri("/").rstrip("/")
        return_to = self.request.build_absolute_uri(reverse("accounts:telegram-login"))
        return telegram_login_url(bot_token, origin, return_to)


def lockout(request: HttpRequest, response: object, credentials: object) -> HttpResponse:
    """The page django-axes shows instead of the login form while a login is locked out."""
    context = {
        "failure_limit": settings.AXES_FAILURE_LIMIT,
        "minutes": int(settings.AXES_COOLOFF_TIME.total_seconds() // 60),
    }
    return render(request, "accounts/lockout.html", context, status=HTTPStatus.TOO_MANY_REQUESTS)


class PasswordResetView(auth_views.PasswordResetView):
    """Sends the reset letter as text and in the panel style, signed with the panel name."""

    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/email/password_reset.txt"
    html_email_template_name = "accounts/email/password_reset.html"
    subject_template_name = "accounts/email/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password-reset-sent")

    def form_valid(self, form: Any) -> HttpResponse:
        self.extra_email_context = {"site_name": cached_settings.get().site_name}
        return super().form_valid(form)


class PasswordResetConfirmView(SuccessMessageMixin, auth_views.PasswordResetConfirmView):  # type: ignore[type-arg]
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:login")
    success_message = gettext_lazy("New password saved. Sign in with it.")


class TwoFactorView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    """A page that takes a code from the authenticator app."""

    form_class = TokenForm

    @property
    def two_factor(self) -> TwoFactor:
        return TwoFactor(cast(User, self.request.user))

    def next_url(self) -> str:
        url = self.request.POST.get("next") or self.request.GET.get("next", "")
        if url_has_allowed_host_and_scheme(
            url, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
        ):
            return url
        return resolve_url("home")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(next=self.next_url(), **kwargs)

    def wrong_code(self, form: TokenForm) -> HttpResponse:
        form.add_error("token", _("Wrong code. Check that the phone shows the right time."))
        return self.form_invalid(form)

    def passed(self, device: Device) -> HttpResponse:
        otp_login(self.request, device)
        return redirect(self.next_url())

    def new_backup_codes(self) -> HttpResponse:
        """Issues a new set of backup codes and shows it, the only time the codes are seen."""
        user = cast(User, self.request.user)
        codes = BackupCodes(user).issue()
        audit.log_request(self.request, "accounts.backup_codes_issue", user)
        context = {"codes": codes, "next": self.next_url()}
        return render(self.request, "accounts/backup_codes_new.html", context)


class TwoFactorVerifyView(TwoFactorView):
    """The second step of signing in."""

    template_name = "accounts/two_factor_verify.html"
    form_class = SignInCodeForm

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if session_verified(cast(User, request.user)) or not self.two_factor.enabled:
            return redirect(self.next_url())
        return super().get(request, *args, **kwargs)

    def form_valid(self, form: TokenForm) -> HttpResponse:
        device = self.two_factor.sign_in(form.cleaned_data["token"])
        if device is None:
            return self.wrong_code(form)
        if isinstance(device, StaticDevice):
            self.backup_code_used()
        return self.passed(device)

    def backup_code_used(self) -> None:
        user = cast(User, self.request.user)
        left = BackupCodes(user).left
        audit.log_request(self.request, "accounts.backup_code_use", user, left=left)
        messages.warning(
            self.request,
            _("You signed in with a backup code. Backup codes left: %(left)d.") % {"left": left},
        )


class TwoFactorSetupView(TwoFactorView):
    """Connecting an authenticator app. A new one replaces the old one."""

    template_name = "accounts/two_factor_setup.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        two_factor = self.two_factor
        return super().get_context_data(
            enrollment=two_factor.enrollment(), required=two_factor.required, **kwargs
        )

    def form_valid(self, form: TokenForm) -> HttpResponse:
        device = self.two_factor.confirm(form.cleaned_data["token"])
        if device is None:
            return self.wrong_code(form)
        messages.success(self.request, _("Two-factor authentication is on."))
        otp_login(self.request, device)
        return self.new_backup_codes()


class BackupCodesView(TwoFactorView):
    """A new set of backup codes, confirmed by a code from the app; the old codes stop working."""

    template_name = "accounts/backup_codes.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if isinstance(request.user, User) and not self.two_factor.enabled:
            return to_profile("two-factor")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        left = BackupCodes(cast(User, self.request.user)).left
        return super().get_context_data(left=left, **kwargs)

    def form_valid(self, form: TokenForm) -> HttpResponse:
        if self.two_factor.verify(form.cleaned_data["token"]) is None:
            return self.wrong_code(form)
        return self.new_backup_codes()


class TwoFactorDisableView(TwoFactorView):
    """Turning the second factor off takes a current code; not allowed where it is required."""

    template_name = "accounts/two_factor_disable.html"
    success_url = format_lazy("{}#two-factor", reverse_lazy("accounts:profile"))

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if isinstance(request.user, User) and self.two_factor.required:
            messages.error(request, _("Two-factor authentication is required for administrators."))
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: TokenForm) -> HttpResponse:
        two_factor = self.two_factor
        if two_factor.verify(form.cleaned_data["token"]) is None:
            return self.wrong_code(form)
        two_factor.disable()
        messages.success(self.request, _("Two-factor authentication is off."))
        return super().form_valid(form)
