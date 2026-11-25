"""Model fields and validators shared by the apps: a link people follow from the panel."""

from typing import TYPE_CHECKING, Any

from django.core.validators import URLValidator
from django.db import models

# Links people follow from the panel: no javascript:, data: or ftp: addresses.
http_url = URLValidator(schemes=["http", "https"])


if TYPE_CHECKING:
    _URLField = models.URLField[str, str]
else:
    _URLField = models.URLField


class LinkField(_URLField):
    """A link whose forms read an address typed without a scheme as https://."""

    def formfield(self, **kwargs: Any) -> Any:
        return super().formfield(**{"assume_scheme": "https", **kwargs})

    def deconstruct(self) -> Any:
        """Migrations see a plain URLField: the scheme matters to forms only."""
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.URLField", args, kwargs
