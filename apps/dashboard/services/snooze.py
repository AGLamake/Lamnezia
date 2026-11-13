"""Notices of the overview a staff member hid until tomorrow; kept in the session, one day long."""

import re
from datetime import date

from django.contrib.sessions.backends.base import SessionBase

from apps.dashboard.services.attention import Notice

SESSION_KEY = "hidden_notices"
# The keys `Attention` gives its notices: a kind and, for a server, its id.
_KEY = re.compile(r"[a-z-]{1,32}(:\d{1,10})?")


class Snooze:
    """Which notices are hidden today; a notice hidden yesterday shows again."""

    def __init__(self, session: SessionBase, today: date) -> None:
        self._session = session
        self._today = today.isoformat()

    @staticmethod
    def valid(key: str) -> bool:
        return _KEY.fullmatch(key) is not None

    def hidden(self) -> set[str]:
        stored = self._session.get(SESSION_KEY, {})
        return {key for key, day in stored.items() if day == self._today}

    def hide(self, key: str) -> None:
        self._session[SESSION_KEY] = {key: self._today for key in self.hidden() | {key}}

    def show_all(self) -> None:
        self._session.pop(SESSION_KEY, None)

    def split(self, notices: list[Notice]) -> tuple[list[Notice], int]:
        """The notices left to show and how many are hidden."""
        hidden = self.hidden()
        shown = [notice for notice in notices if notice.key not in hidden]
        return shown, len(notices) - len(shown)
