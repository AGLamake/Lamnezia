"""Links from an operation to the page of its object (server, key) by the id in its parameters."""

from collections.abc import Callable

from apps.operations.models import Operation

# Takes the id from the parameters, returns the page address or None (the object is gone).
Resolver = Callable[[int], str | None]


class SubjectLinks:
    """Apps say which parameter holds the id of their object and how to get its page.

    The most specific parameter wins: a key operation also names its server, so `key_id` is
    registered with a higher priority than `server_id`. Usage::

        subjects.register("server_id", lambda pk: reverse("servers:detail", args=[pk]))
    """

    def __init__(self) -> None:
        self._resolvers: list[tuple[int, str, Resolver]] = []

    def register(self, param: str, resolver: Resolver, *, priority: int = 0) -> None:
        self._resolvers.append((priority, param, resolver))
        self._resolvers.sort(key=lambda entry: -entry[0])

    def unregister(self, param: str) -> None:
        self._resolvers = [entry for entry in self._resolvers if entry[1] != param]

    def url(self, operation: Operation) -> str | None:
        for _priority, param, resolver in self._resolvers:
            pk = operation.params.get(param)
            if isinstance(pk, int):
                return resolver(pk)
        return None


subjects = SubjectLinks()
