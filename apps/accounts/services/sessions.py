"""The browsers a user is signed in on: recorded at sign-in, listed in the profile, ended there.

A row keeps the session key, the browser and system names and the sign-in time; no address and
no pages. Whether a session still works is asked of Django's session store.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.contrib.sessions.models import Session
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.accounts.models import User, UserSession
from apps.guides.services.devices import DetectedDevice

# The first match names the browser: Edge and Opera also say Chrome, Chrome also says Safari.
_BROWSERS = (
    ("Edge", r"Edg(e|A|iOS)?/"),
    ("Opera", r"OPR/|Opera"),
    ("Yandex Browser", r"YaBrowser/"),
    ("Firefox", r"Firefox/|FxiOS/"),
    ("Chrome", r"Chrome/|CriOS/"),
    ("Safari", r"Safari/"),
)
_DEVICE_LENGTH = UserSession._meta.get_field("device").max_length or 100


def device_name(user_agent: str) -> str:
    """ "Firefox · Windows" from a User-Agent header; an unknown part is left out."""
    browser = next((name for name, pattern in _BROWSERS if re.search(pattern, user_agent)), "")
    device = DetectedDevice.from_user_agent(user_agent).device
    parts = [part for part in (browser, device.label if device else "") if part]
    return " · ".join(str(part) for part in parts)[:_DEVICE_LENGTH]


@dataclass(frozen=True)
class SignedIn:
    """A live session of the list; `current` is the one the page is opened in. The page names a
    session by `id`: the session key itself signs in, so it never leaves the server.
    """

    id: int
    device: str
    signed_in_at: datetime
    current: bool


class UserSessions:
    """The sessions of one user."""

    def __init__(self, user: User) -> None:
        self._user = user

    def record(self, request: HttpRequest) -> None:
        """Remember the session of this request, if it is not remembered yet."""
        key = request.session.session_key
        if not key:
            return
        agent = request.headers.get("User-Agent", "")
        UserSession.objects.update_or_create(
            session_key=key, defaults={"user": self._user, "device": device_name(agent)}
        )

    def live(self, request: HttpRequest) -> list[SignedIn]:
        """Sessions that still work, the current one first; rows of ended ones are dropped."""
        self.record(request)
        rows = list(self._user.sessions.all())
        alive = set(
            Session.objects.filter(
                session_key__in=[row.session_key for row in rows], expire_date__gt=timezone.now()
            ).values_list("session_key", flat=True)
        )
        current = request.session.session_key
        if current:
            alive.add(current)
        self._user.sessions.exclude(session_key__in=alive).delete()
        found = [
            SignedIn(row.pk, row.device, row.signed_in_at, row.session_key == current)
            for row in rows
            if row.session_key in alive
        ]
        return sorted(found, key=lambda session: not session.current)

    def end(self, session_id: int, current: str | None) -> bool:
        """End one other session of the user; False when there is no such session of theirs."""
        row = self._user.sessions.exclude(session_key=current or "").filter(pk=session_id).first()
        if row is None:
            return False
        with transaction.atomic():
            Session.objects.filter(session_key=row.session_key).delete()
            row.delete()
        return True

    def end_others(self, current: str | None) -> int:
        """End every session of the user but `current`; how many ended."""
        keys = list(
            self._user.sessions.exclude(session_key=current or "").values_list(
                "session_key", flat=True
            )
        )
        with transaction.atomic():
            Session.objects.filter(session_key__in=keys).delete()
            self._user.sessions.filter(session_key__in=keys).delete()
        return len(keys)


def _signed_in(sender: object, request: HttpRequest | None, user: User, **kwargs: Any) -> None:
    if request is not None and hasattr(request, "session"):
        UserSessions(user).record(request)


def _signed_out(sender: object, request: HttpRequest | None, **kwargs: Any) -> None:
    key = request.session.session_key if request is not None else None
    if key:
        UserSession.objects.filter(session_key=key).delete()


def connect() -> None:
    user_logged_in.connect(_signed_in, dispatch_uid="accounts.sessions.signed_in")
    user_logged_out.connect(_signed_out, dispatch_uid="accounts.sessions.signed_out")
