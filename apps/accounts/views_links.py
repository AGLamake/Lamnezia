"""Linked Telegram and Discord accounts: linking, unlinking and signing in with them."""

import secrets
from typing import cast

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import Messenger, User
from apps.accounts.services.link_offer import LinkOffer
from apps.accounts.services.linking import AccountLinks, LinkError
from apps.accounts.services.messengers import (
    DiscordOAuth,
    MessengerAuthError,
    MessengerIdentity,
    TelegramLoginCheck,
)
from apps.accounts.views_profile import to_profile
from apps.audit.services import journal as audit
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings

_BACKEND = "django.contrib.auth.backends.ModelBackend"
_DISCORD_STATE = "accounts:discord-oauth"
LINK, LOGIN = "link", "login"


def discord_oauth(request: HttpRequest) -> DiscordOAuth:
    site = cached_settings.get()
    redirect_uri = request.build_absolute_uri(reverse("accounts:discord-callback"))
    return DiscordOAuth(site.discord_client_id, site.discord_client_secret, redirect_uri)


def _sign_in(
    request: HttpRequest, messenger: Messenger, identity: MessengerIdentity
) -> HttpResponse:
    """Sign in the user this messenger account is linked to; never makes new accounts."""
    user = AccountLinks.user_of(messenger, identity.external_id)
    if user is None:
        messages.error(
            request, _("No account of the panel is linked to this one. Sign in with a password.")
        )
        return redirect("accounts:login")
    login(request, user, backend=_BACKEND)
    audit.log_request(request, "accounts.messenger_login", user, messenger=messenger)
    return redirect("home")


class UnlinkView(LoginRequiredMixin, View):
    messenger = Messenger.TELEGRAM

    def post(self, request: HttpRequest) -> HttpResponse:
        user = cast(User, request.user)
        if AccountLinks().unlink(user, self.messenger):
            audit.log_request(request, "accounts.unlink", user, messenger=self.messenger)
            messages.success(request, _("Unlinked."))
        return to_profile("messengers")


class LinkOfferDismissView(LoginRequiredMixin, View):
    """«Not now» on the offer to link a messenger (accounts/_link_offer.html)."""

    def post(self, request: HttpRequest) -> HttpResponse:
        LinkOffer(cast(User, request.user), cached_settings.get()).dismiss()
        if request.headers.get("HX-Request"):
            return HttpResponse()
        return redirect("portal:index")


class DiscordLoginView(View):
    """Sends a guest to Discord to sign in with the Discord account linked to them."""

    purpose = LOGIN

    def allowed(self, site: SiteSettings) -> bool:
        return site.discord_login and not isinstance(self.request.user, User)

    def get(self, request: HttpRequest) -> HttpResponseBase:
        site = cached_settings.get()
        if not (site.discord_oauth and self.allowed(site)):
            raise Http404
        state = secrets.token_urlsafe(24)
        request.session[_DISCORD_STATE] = {"state": state, "purpose": self.purpose}
        return redirect(discord_oauth(request).authorize_url(state))


class DiscordConnectView(LoginRequiredMixin, DiscordLoginView):
    """Sends a signed-in user to Discord to link their Discord account."""

    purpose = LINK

    def allowed(self, site: SiteSettings) -> bool:
        return True


class DiscordCallbackView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        # The state is taken out of the session at once: a callback URL works only once.
        started = request.session.pop(_DISCORD_STATE, None) or {}
        identity = self._identity(request, started.get("state", ""))
        if identity is None:
            messages.error(request, _("Discord did not confirm the account. Try again."))
            if isinstance(request.user, User):
                return to_profile("messengers")
            return redirect("accounts:login")
        if started.get("purpose") == LOGIN:
            return _sign_in(request, Messenger.DISCORD, identity)
        return _link(request, Messenger.DISCORD, identity)

    @staticmethod
    def _identity(request: HttpRequest, expected_state: str) -> MessengerIdentity | None:
        state, code = request.GET.get("state", ""), request.GET.get("code", "")
        if not (state and code and secrets.compare_digest(state.encode(), expected_state.encode())):
            return None
        try:
            return discord_oauth(request).identity(code)
        except MessengerAuthError:
            return None


def _link(request: HttpRequest, messenger: Messenger, identity: MessengerIdentity) -> HttpResponse:
    if not isinstance(request.user, User):
        return redirect("accounts:login")
    try:
        account = AccountLinks().link(
            request.user, messenger, identity.external_id, identity.username
        )
    except LinkError as error:
        messages.error(request, str(error))
    else:
        audit.log_request(request, "accounts.link", account, messenger=messenger)
        messages.success(request, _("Linked."))
    return to_profile("messengers")


class TelegramLoginView(View):
    """The address oauth.telegram.org returns a visitor to after Telegram confirmed them.

    Telegram puts the signed data in the `#tgAuthResult` fragment, which never reaches the
    server: the bare address renders a page whose script (static/js/telegram-login.js) moves the
    data into the query and opens the address again for the check.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        site = cached_settings.get()
        if not (site.telegram_login and site.telegram_linking):
            raise Http404
        if not request.GET:
            return render(request, "accounts/telegram_return.html")
        check = TelegramLoginCheck(
            site.telegram_bot_token, max_age=settings.MESSENGER_LOGIN_MAX_AGE
        )
        try:
            identity = check.identity(request.GET.dict())
        except MessengerAuthError:
            messages.error(request, _("Telegram did not confirm the account. Try again."))
            return redirect("accounts:login")
        return _sign_in(request, Messenger.TELEGRAM, identity)
