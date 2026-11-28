"""One reading of an object at a time."""

from django.core.cache.backends.locmem import LocMemCache

from apps.servers.services.poll_guard import PollGuard


def _guard(key: str = "server:1") -> PollGuard:
    return PollGuard(key, hold=60, backend=LocMemCache("guard", {}))


def test_the_reading_runs_and_gives_its_result() -> None:
    assert _guard().run(lambda: 42) == 42


def test_a_second_reading_of_the_same_object_is_skipped_meanwhile() -> None:
    guard = _guard()
    inner: list[object] = []

    guard.run(lambda: inner.append(guard.run(lambda: "late")))

    assert inner == [None]
    assert guard.run(lambda: "next") == "next"


def test_other_objects_are_read_meanwhile() -> None:
    backend = LocMemCache("guards", {})
    first = PollGuard("server:1", hold=60, backend=backend)
    second = PollGuard("relay:1", hold=60, backend=backend)

    assert first.run(lambda: second.run(lambda: "other")) == "other"


def test_a_key_taken_over_after_it_expired_is_left_to_its_reading() -> None:
    backend = LocMemCache("expired", {})
    guard = PollGuard("server:1", hold=60, backend=backend)

    def outlived() -> None:
        backend.set("poll:server:1", "later reading")

    guard.run(outlived)

    assert backend.get("poll:server:1") == "later reading"
