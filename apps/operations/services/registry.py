"""Registry of operation kinds: the code behind `Operation.kind` and its title in the UI."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils.functional import Promise

if TYPE_CHECKING:
    from apps.operations.services.context import OperationContext

Handler = Callable[["OperationContext"], None]


@dataclass(frozen=True)
class OperationKind:
    """A kind of operation. The handler must be idempotent or safe to run again (DESIGN.md 7)."""

    name: str
    title: str | Promise
    handler: Handler


class UnknownOperationKindError(LookupError):
    pass


class OperationRegistry:
    """Kinds are registered when their app is imported, typically from `AppConfig.ready()`.

    Usage::

        @operations.register("servers.check", _("Check server"))
        def check_server(ctx: OperationContext) -> None: ...
    """

    def __init__(self) -> None:
        self._kinds: dict[str, OperationKind] = {}

    def register(self, name: str, title: str | Promise) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            if name in self._kinds:
                raise ValueError(f"operation kind {name!r} is already registered")
            self._kinds[name] = OperationKind(name, title, handler)
            return handler

        return decorator

    def unregister(self, name: str) -> None:
        del self._kinds[name]

    def get(self, name: str) -> OperationKind:
        try:
            return self._kinds[name]
        except KeyError:
            raise UnknownOperationKindError(name) from None

    def title(self, name: str) -> str:
        """Title for the UI; the raw name if the kind is gone (e.g. after an upgrade)."""
        kind = self._kinds.get(name)
        return str(kind.title) if kind else name


operations = OperationRegistry()
