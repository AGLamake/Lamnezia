"""Pages for guests: registration, email confirmation and signing in by a link from a letter."""

from http import HTTPStatus
from typing import Any

from django.contrib import messages
from django.contrib.auth import login
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView

from apps.accounts.forms import MagicLinkForm, RegistrationForm
from apps.accounts.models import Status, User
from apps.accounts.services.letters import Letters
from apps.accounts.services.registration import Applicant, Registration, RegistrationError
from apps.accounts.services.tokens import email_confirmation, magic_link
from apps.audit.services import journal as audit
from apps.core.mail import mail_configured
from apps.core.services.site_settings import cached_settings

# Accounts made here have a password; the backend name lets login() skip authenticate().
_BACKEND = "django.contrib.auth.backends.ModelBackend"


def registration(request: HttpRequest) -> Registration:
    letters = Letters(request.build_absolute_uri("/"))
    return Registration(cached_settings.get(), letters, ip=audit.client_ip(request))


def _refused(request: HttpRequest, message: str) -> HttpResponse:
    context = {"message": message}
    return render(
        request, "accounts/registration_closed.html", context, status=HTTPStatus.FORBIDDEN
    )


def _sign_in(request: HttpRequest, user: User) -> HttpResponse:
    login(request, user, backend=_BACKEND)
    return redirect("home")


class RegisterView(FormView):  # type: ignore[type-arg]
    """Open registration at /register/, by invite at /invite/<code>/."""

    form_class = RegistrationForm
    template_name = "accounts/register.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if isinstance(request.user, User):
            return redirect("home")
        self.registration = registration(request)
        self.code: str | None = kwargs.get("code")
        try:
            invite = self.registration.invite(self.code) if self.code else None
        except RegistrationError as error:
            return _refused(request, str(error))
        if invite is None and not self.registration.is_open:
            return _refused(request, _("Registration is by invitation only."))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(invited=bool(self.code), **kwargs)

    def form_valid(self, form: RegistrationForm) -> HttpResponse:
        data = form.cleaned_data
        applicant = Applicant(data["username"], data["email"], data["password1"])
        try:
            user = self.registration.register(applicant, self.code)
        except RegistrationError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        if user.is_active:
            return _sign_in(self.request, user)
        return render(self.request, "accounts/register_done.html", {"email": user.email})


class ConfirmEmailView(View):
    def get(self, request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
        user = _unconfirmed_user(uidb64)
        if user is None or not email_confirmation.check_token(user, token):
            return _refused(request, _("The confirmation link is wrong or has expired."))
        registration(request).confirm_email(user)
        messages.success(request, _("Email confirmed."))
        return _sign_in(request, user)


def _unconfirmed_user(uidb64: str) -> User | None:
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
    except ValueError:
        return None
    # A blocked account is inactive too, but a confirmation link must not open it again.
    users = User.objects.filter(is_active=False).exclude(status=Status.BLOCKED)
    return users.filter(pk=pk).first() if pk.isdigit() else None


class MagicLinkRequestView(FormView):  # type: ignore[type-arg]
    """Sends a sign-in link; the answer is the same whether the account exists or not."""

    form_class = MagicLinkForm
    template_name = "accounts/magic_link.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not mail_configured():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: MagicLinkForm) -> HttpResponse:
        letters = Letters(self.request.build_absolute_uri("/"))
        for user in User.objects.filter(email__iexact=form.cleaned_data["email"], is_active=True):
            letters.magic_link(user)
        messages.info(self.request, _("If the account exists, a sign-in link is on its way."))
        return redirect("accounts:login")


class MagicLoginView(View):
    """The link opens a button: mail scanners that follow links must not spend it."""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        user = magic_link.user(token)
        if user is None:
            return _refused(request, _("The sign-in link is wrong, used or has expired."))
        return render(request, "accounts/magic_login.html", {"account": user})

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        user = magic_link.user(token)
        if user is None:
            return _refused(request, _("The sign-in link is wrong, used or has expired."))
        return _sign_in(request, user)
