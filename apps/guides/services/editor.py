"""Editing FAQ articles and useful materials from the panel: save, publish, order, delete and
restore the deleted built-in ones; every change goes to the audit journal.
"""

from dataclasses import dataclass
from typing import Any

from django.db import models, transaction
from django.db.models import Max
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.guides.models import BuiltinEntry, FaqArticle, UsefulMaterial
from apps.guides.services.builtin import BuiltinContent

Entry = FaqArticle | UsefulMaterial

# A new entry goes this far below the last one, so one can later be put between two by hand.
_POSITION_STEP = 10


@dataclass(frozen=True)
class Shelf:
    """One kind of guide entry the panel edits: its address part, model and audit noun."""

    slug: str
    model: type[Entry]
    noun: str
    title: Promise

    def entries(self) -> models.QuerySet[Entry]:
        return self.model._default_manager.order_by("position", "id")


FAQ = Shelf("faq", FaqArticle, "article", _("FAQ"))
MATERIALS = Shelf("materials", UsefulMaterial, "material", _("Useful materials"))
SHELVES = {shelf.slug: shelf for shelf in (FAQ, MATERIALS)}


def shelf_of(entry: Entry) -> Shelf:
    return FAQ if isinstance(entry, FaqArticle) else MATERIALS


class GuideEditor:
    """Changes of one staff member to the FAQ and the useful materials."""

    def __init__(self, member: User, ip: str | None = None) -> None:
        self._member = member
        self._ip = ip

    def save(self, entry: Entry) -> Entry:
        """Create or update an entry; a new one goes to the end of its list."""
        created = entry._state.adding
        if created:
            last = shelf_of(entry).entries().aggregate(last=Max("position"))["last"]
            entry.position = (last or 0) + _POSITION_STEP
        entry.save()
        self._audit(entry, "create" if created else "update")
        return entry

    def publish(self, entry: Entry, published: bool) -> None:
        """Show the entry on the public pages or hide it there; it stays in the panel."""
        if entry.is_published == published:
            return
        entry.is_published = published
        entry.save(update_fields=["is_published"])
        self._audit(entry, "publish" if published else "hide")

    def move(self, entry: Entry, step: int) -> None:
        """Swap the entry with its neighbour above (`step=-1`) or below (`step=1`).

        The list is numbered again in the order shown, so entries with equal positions move too.
        """
        with transaction.atomic():
            entries = list(shelf_of(entry).entries().select_for_update())
            index = next(i for i, item in enumerate(entries) if item.pk == entry.pk)
            target = index + step
            if not 0 <= target < len(entries):
                return
            entries[index], entries[target] = entries[target], entries[index]
            changed = []
            for number, item in enumerate(entries, start=1):
                if item.position != number * _POSITION_STEP:
                    item.position = number * _POSITION_STEP
                    changed.append(item)
            manager: models.Manager[Any] = type(entry)._default_manager
            manager.bulk_update(changed, ["position"])
        self._audit(entry, "move", direction="up" if step < 0 else "down")

    def delete(self, entry: Entry) -> None:
        """A built-in entry deleted here is listed as deleted and can be restored."""
        self._audit(entry, "delete")
        entry.delete()

    def restore(self, offered: BuiltinEntry) -> bool:
        """Bring a deleted built-in entry back as this version ships it."""
        restored = BuiltinContent().restore([offered]) > 0
        if restored:
            self._log("guides.builtin_restore", offered, slug=offered.slug)
        return restored

    def _audit(self, entry: Entry, verb: str, **data: str) -> None:
        self._log(f"guides.{shelf_of(entry).noun}_{verb}", entry, **data)

    def _log(self, action: str, target: models.Model, **data: str) -> None:
        audit.log(action, actor=self._member, target=target, data=data, ip=self._ip)
