from collections.abc import Iterator
from contextlib import nullcontext
from typing import Any

import pytest
from django.conf import settings

from apps.traffic.services.poller import TrafficPoller
from apps.traffic.services.targets import PollTarget, poll_targets
from apps.traffic.tasks import collect_server, collect_traffic

pytestmark = pytest.mark.django_db


def target(name: str) -> PollTarget:
    return PollTarget(name, nullcontext, {f"awg:{name}": "amnezia-awg2"})  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _targets() -> Iterator[None]:
    def provider() -> list[PollTarget]:
        return [target("a"), target("b")]

    poll_targets.register(provider)
    yield
    poll_targets.unregister(provider)


def test_every_server_is_polled_by_a_task_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    queued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        collect_server, "apply_async", lambda args, **options: queued.append((args, options))
    )

    collect_traffic()

    assert [args for args, _options in queued] == [("a",), ("b",)]
    assert {options["expires"] for _args, options in queued} == {settings.TRAFFIC_INTERVAL}


def test_a_task_polls_only_its_server(monkeypatch: pytest.MonkeyPatch) -> None:
    polled: list[str] = []
    monkeypatch.setattr(TrafficPoller, "poll", lambda self, target: polled.append(target.name))

    collect_server("b")
    collect_server("removed meanwhile")

    assert polled == ["b"]
