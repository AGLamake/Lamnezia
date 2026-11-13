"""Built-in FAQ articles and useful materials added on start: only those this panel never had.

An entry the admin deleted can be brought back from the fixture on request.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from django.core import serializers
from django.db import models, transaction

from apps.guides.models import BuiltinEntry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
BUILTIN_FIXTURES = ("faq.json", "materials.json")


@dataclass(frozen=True)
class BuiltinContent:
    """Adds fixture entries by slug once; edited and deleted ones are left as the admin left them.

    Every slug offered is remembered in `BuiltinEntry`, so an entry the admin deleted does not come
    back, while an entry new in a later version is added on the next start.
    """

    fixtures: Iterable[Path] = tuple(FIXTURES / name for name in BUILTIN_FIXTURES)

    def load(self) -> int:
        """Add what is missing; return how many entries were created."""
        with transaction.atomic():
            return sum(self._offer(entry) for entry in self._entries())

    def restore(self, entries: Iterable[BuiltinEntry]) -> int:
        """Create the deleted entries again as the fixture has them; return how many came back.

        One the panel has again, or one a later version no longer ships, is skipped.
        """
        shipped = {self._key(entry): entry for entry in self._entries()}
        restored = 0
        with transaction.atomic():
            for offered in entries:
                entry = shipped.get((offered.model, offered.slug))
                if entry is None or type(entry)._default_manager.filter(slug=offered.slug).exists():
                    continue
                entry.pk = None
                entry.save()
                restored += 1
        return restored

    def _entries(self) -> list[models.Model]:
        return [entry for path in self.fixtures for entry in self._read(path)]

    @staticmethod
    def _key(entry: models.Model) -> tuple[str, str]:
        return type(entry)._meta.label_lower, entry.slug  # type: ignore[attr-defined]

    @staticmethod
    def _read(path: Path) -> list[models.Model]:
        with path.open(encoding="utf-8") as stream:
            return [item.object for item in serializers.deserialize("json", stream)]

    @staticmethod
    def _offer(entry: models.Model) -> bool:
        model = type(entry)
        slug = entry.slug  # type: ignore[attr-defined]
        _, first_time = BuiltinEntry.objects.get_or_create(model=model._meta.label_lower, slug=slug)
        if not first_time or model._default_manager.filter(slug=slug).exists():
            return False
        entry.pk = None
        entry.save()
        return True
