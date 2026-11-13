from typing import Any

import pytest
from django.conf import settings

from apps.servers.services.monitor import ServerMonitor
from apps.servers.tasks import monitor_server, monitor_servers
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def test_every_server_is_read_by_a_task_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = ServerFactory.create_batch(2)
    queued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        monitor_server, "apply_async", lambda args, **options: queued.append((args, options))
    )

    monitor_servers()

    assert sorted(args for args, _options in queued) == [(server.pk,) for server in servers]
    assert {options["expires"] for _args, options in queued} == {settings.MONITOR_INTERVAL}


def test_a_server_deleted_meanwhile_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    polled: list[Any] = []
    monkeypatch.setattr(ServerMonitor, "poll", lambda self, server: polled.append(server))

    monitor_server(404)

    assert polled == []
