"""The offer to link Telegram or Discord: which messengers the panel can link and when to offer."""

from collections.abc import Callable
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Messenger, User
from apps.core.models import SiteSettings


def linkable(site: SiteSettings) -> list[Messenger]:
    """The messengers a user can link now: the administrator has set up their bot."""
    messengers = []
    if site.telegram_linking:
        messengers.append(Messenger.TELEGRAM)
    if site.discord_oauth:
        messengers.append(Messenger.DISCORD)
    return messengers


def named(messengers: list[Messenger]) -> str:
    """The messengers for a sentence: "Telegram", "Discord" or "Telegram or Discord"."""
    labels = [str(messenger.label) for messenger in messengers]
    match labels:
        case [first, second]:
            return _("%(first)s or %(second)s") % {"first": first, "second": second}
        case _:
            return "".join(labels)


class LinkOffer:
    """The card on «My access» that offers a user without linked messengers to link one.

    "Not now" hides it for `PAUSE`; after the second time it is not offered again.
    """

    PAUSE = timedelta(days=30)
    DISMISSALS = 2

    def __init__(
        self, user: User, site: SiteSettings, clock: Callable[[], datetime] = timezone.now
    ) -> None:
        self._user = user
        self._site = site
        self._clock = clock

    def messengers(self) -> list[Messenger]:
        """What to offer; empty when the card is not shown."""
        user = self._user
        if user.link_offer_dismissals >= self.DISMISSALS:
            return []
        if user.link_offer_hidden_until and user.link_offer_hidden_until > self._clock():
            return []
        if user.linked_accounts.exists():
            return []
        return linkable(self._site)

    def context(self) -> dict[str, object]:
        """What accounts/_link_offer.html takes."""
        messengers = self.messengers()
        return {"link_offer": messengers, "link_offer_names": named(messengers)}

    def dismiss(self) -> None:
        user = self._user
        user.link_offer_dismissals += 1
        user.link_offer_hidden_until = self._clock() + self.PAUSE
        user.save(update_fields=["link_offer_dismissals", "link_offer_hidden_until"])
