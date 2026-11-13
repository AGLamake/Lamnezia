"""The profile: account and email, password, two-factor, messengers, notifications and sessions
on one page. Each section posts to its own view; a form with errors shows the page again.
"""

from typing import Any, cast

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.forms import EmailChangeForm, NotifyViaForm
from apps.accounts.models import Messenger, User
from apps.accounts.services.backup_codes import BackupCodes
from apps.accounts.services.letters import Letters
from apps.accounts.services.linking import AccountLinks, invitation, links_by_code
from apps.accounts.services.sessions import UserSessions
from apps.accounts.services.tokens import email_change
from apps.accounts.services.two_factor import TwoFactor
from apps.audit.services import journal as audit
from apps.core.mail import mail_configured
from apps.core.services.site_settings import cached_settings

_TEMPLATE = "accounts/profile.html"


def to_profile(section: str) -> HttpResponseRedirect:
    """The profile page scrolled to `section`."""
    return redirect(f"{reverse('accounts:profile')}#{section}")


class SectionRedirect(LoginRequiredMixin, View):
    """An old address of a profile section: the security and messenger pages were separate."""

    section = ""

    def get(self, request: HttpRequest) -> HttpResponseRedirect:
        return to_profile(self.section)


class ProfileMixin(LoginRequiredMixin):
    """Builds the profile page; a section view passes its bound form to show its errors."""

    template_name: str | None = _TEMPLATE
    request: HttpRequest

    @property
    def member(self) -> User:
        return cast(User, self.request.user)

    def profile_context(self, **sections: Any) -> dict[str, Any]:
        user = self.member
        linked = {account.messenger: account for account in user.linked_accounts.all()}
        context = {
            "layout": "layouts/admin.html" if user.is_staff_member else "layouts/portal.html",
            "site": cached_settings.get(),
            "mail": mail_configured(),
            "two_factor": TwoFactor(user),
            "backup_codes": BackupCodes(user),
            "telegram": linked.get(Messenger.TELEGRAM),
            "discord": linked.get(Messenger.DISCORD),
            "code_minutes": settings.LINK_CODE_MINUTES,
            "sessions": UserSessions(user).live(self.request),
            "email_form": EmailChangeForm(user),
            "password_form": self._password_form(PasswordChangeForm(user)),
            "notify_form": NotifyViaForm(instance=user),
        }
        return context | sections

    @staticmethod
    def _password_form(form: PasswordChangeForm) -> PasswordChangeForm:
        """Django focuses the old password; on a page of sections that scrolls past the top."""
        form.fields["old_password"].widget.attrs.pop("autofocus", None)
        return form

    def render_profile(self, **sections: Any) -> HttpResponse:
        return render(self.request, _TEMPLATE, self.profile_context(**sections))


class ProfileView(ProfileMixin, TemplateView):
    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(**self.profile_context(), **kwargs)


class _SectionView(ProfileMixin, View):
    """A form of one section: it is only posted, the page itself is the profile."""

    http_method_names = ["post"]  # noqa: RUF012


class EmailChangeView(_SectionView):
    """With mail set up the new address gets a link first; without it the address changes now."""

    def post(self, request: HttpRequest) -> HttpResponse:
        user = self.member
        form = EmailChangeForm(user, request.POST)
        if not form.is_valid():
            return self.render_profile(email_form=form)
        email = form.cleaned_data["email"]
        if mail_configured():
            Letters(request.build_absolute_uri("/")).change_email(user, email)
            messages.info(
                request,
                _("We sent a link to %(email)s. The email changes when you open it.")
                % {"email": email},
            )
        else:
            user.email = email
            user.save(update_fields=["email"])
            audit.log_request(request, "accounts.email_change", user)
            messages.success(request, _("Email changed."))
        return to_profile("account")


class EmailConfirmView(LoginRequiredMixin, View):
    """The link from the letter to the new address."""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        user = cast(User, request.user)
        if email_change.apply(token, user) is None:
            messages.error(request, _("The link is out of date or is for another account."))
        else:
            audit.log_request(request, "accounts.email_change", user)
            messages.success(request, _("Email changed."))
        return to_profile("account")


class PasswordChangeView(ProfileMixin, auth_views.PasswordChangeView):
    """A new password ends the other sessions: whoever knew the old one is signed out."""

    success_url = reverse_lazy("accounts:profile")

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return to_profile("password")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return context | self.profile_context(password_form=self._password_form(context["form"]))

    def form_valid(self, form: PasswordChangeForm) -> HttpResponse:
        super().form_valid(form)
        sessions = UserSessions(self.member)
        sessions.end_others(self.request.session.session_key)
        sessions.record(self.request)
        messages.success(self.request, _("Password changed. Other sessions are signed out."))
        return to_profile("password")


class NotificationsView(_SectionView):
    def post(self, request: HttpRequest) -> HttpResponse:
        form = NotifyViaForm(request.POST, instance=self.member)
        if not form.is_valid():
            return self.render_profile(notify_form=form)
        form.save()
        messages.success(request, _("Saved."))
        return to_profile("notifications")


class LinkCodeView(_SectionView):
    """Shows a new single-use code: a Telegram deep link, or `/link <code>` for the Discord bot."""

    messenger = Messenger.TELEGRAM

    def post(self, request: HttpRequest) -> HttpResponse:
        site = cached_settings.get()
        if not links_by_code(site, self.messenger):
            raise Http404
        code = AccountLinks().issue_code(self.member, self.messenger)
        key = "deep_link" if self.messenger == Messenger.TELEGRAM else "discord_command"
        return self.render_profile(code=code, **{key: invitation(site, self.messenger, code)})


class SessionEndView(_SectionView):
    """Ends one other session, or all of them with `session=others`; the current one stays."""

    def post(self, request: HttpRequest) -> HttpResponse:
        sessions = UserSessions(self.member)
        current = request.session.session_key
        chosen = request.POST.get("session", "")
        if chosen == "others":
            ended = sessions.end_others(current)
        else:
            ended = int(chosen.isdigit() and sessions.end(int(chosen), current))
        if ended:
            audit.log_request(request, "accounts.sessions_end", self.member, count=ended)
            text = ngettext("%(count)d session ended.", "%(count)d sessions ended.", ended)
            messages.success(request, text % {"count": ended})
        return to_profile("sessions")
