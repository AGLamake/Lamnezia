"""Outages and lags remembered apart from notifications, so a recovery is told once after one,
even when no channel hears of the trouble itself."""

from apps.notifications.events import Event
from apps.notifications.models import Trouble
from apps.notifications.services.delivery import emit


def report_trouble(event: Event, data: dict[str, str], key: str) -> None:
    """Remember the trouble by `key` and tell it once."""
    Trouble.objects.get_or_create(key=key)
    emit(event, data, key=key)


def report_recovery(trouble_key: str, event: Event, data: dict[str, str], key: str) -> bool:
    """Tell `event` if the trouble of `trouble_key` was going on; True if it was."""
    ended = Trouble.objects.filter(key=trouble_key).delete()[0] > 0
    if ended:
        emit(event, data, key=key)
    return ended


def forget_trouble(key: str) -> None:
    """The trouble is over with nothing to tell: its subject is gone."""
    Trouble.objects.filter(key=key).delete()


def open_troubles(prefix: str) -> list[str]:
    """Keys of the troubles going on that start with `prefix`."""
    return list(Trouble.objects.filter(key__startswith=prefix).values_list("key", flat=True))
