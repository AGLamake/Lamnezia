"""A registry of provider functions: an app adds what it owns without the registry importing it."""

from collections.abc import Callable, Iterable, Iterator


class Providers[T]:
    """Usage: ``@registry.register`` on a function that yields items; iterating yields them all."""

    def __init__(self) -> None:
        self._providers: list[Callable[[], Iterable[T]]] = []

    def register(self, provider: Callable[[], Iterable[T]]) -> Callable[[], Iterable[T]]:
        self._providers.append(provider)
        return provider

    def unregister(self, provider: Callable[[], Iterable[T]]) -> None:
        self._providers.remove(provider)

    def __iter__(self) -> Iterator[T]:
        for provider in self._providers:
            yield from provider()
