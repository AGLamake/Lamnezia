"""A fresh copy after the keys change, and the copies kept when a server is deleted."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.core.cache import cache
from django.db.models import ProtectedError

from amnezia_core.container_archive import ContainerArchive
from amnezia_core.xray import XRAY_CONFIG_PATH
from apps.backups import tasks
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.services import snapshot
from apps.backups.services.freshness import (
    FRESH_COPY_DELAY,
    request_fresh_copy,
    take_fresh_copy,
)
from apps.backups.tests.fake_containers import TarXrayHost
from apps.backups.tests.test_archived import CASES, installed, restore
from apps.keys.services.backends import KeyBackend
from apps.keys.signals import keys_written
from apps.protocols.models import ProtocolInstance
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def scheduled(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """The fresh copies asked for, as (instance id, countdown); none is taken in the test."""
    cache.clear()
    calls: list[tuple[Any, ...]] = []

    def apply_async(args: tuple[int], countdown: int) -> None:
        calls.append((*args, countdown))

    monkeypatch.setattr(tasks.save_protocol_backup, "apply_async", apply_async)
    yield calls
    cache.clear()


def test_source_names_the_instance() -> None:
    assert ProtocolInstance.pk_of("instance:12") == 12
    assert ProtocolInstance.pk_of("awg:1") is None
    assert ProtocolInstance.pk_of("instance:") is None


def test_written_keys_ask_for_one_copy_soon(scheduled: list[tuple[Any, ...]]) -> None:
    keys_written.send(KeyBackend, source="instance:7")
    keys_written.send(KeyBackend, source="instance:7")
    keys_written.send(KeyBackend, source="instance:8")

    assert scheduled == [(7, FRESH_COPY_DELAY), (8, FRESH_COPY_DELAY)]


def test_keys_of_a_source_without_backups_ask_for_nothing(
    scheduled: list[tuple[Any, ...]],
) -> None:
    keys_written.send(KeyBackend, source="awg:1")

    assert scheduled == []


def test_change_after_the_copy_asks_for_the_next_one(scheduled: list[tuple[Any, ...]]) -> None:
    request_fresh_copy(7)
    take_fresh_copy(7)  # no such instance: nothing to read
    request_fresh_copy(7)

    assert scheduled == [(7, FRESH_COPY_DELAY), (7, FRESH_COPY_DELAY)]


def test_fresh_copy_holds_the_key_issued_a_minute_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, protocol, fake = installed(CASES["xray"])
    protocol.save(instance, fake.host())
    assert isinstance(fake, TarXrayHost)
    fake.volume[XRAY_CONFIG_PATH] += b"\n"  # the server config with one more client

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr(snapshot, "connect", connect)

    take_fresh_copy(instance.pk)

    backup = ProtocolBackup.objects.get(instance=instance)
    copy = ContainerArchive.decode(protocol.state, backup.archive)
    assert copy.files()[XRAY_CONFIG_PATH] == fake.volume[XRAY_CONFIG_PATH]


def test_foreign_protocol_container_is_not_started() -> None:
    instance, protocol, fake = installed(CASES["openvpn"])
    protocol.save(instance, fake.host())
    _other, _protocol, rival = installed(CASES["openvpn"])
    rival.container_state = "exited"

    with pytest.raises(RestoreError) as error:
        restore(instance, protocol, rival)

    assert error.value.code == RestoreErrorCode.OTHER_CONTAINER
    assert rival.container_state == "exited"


def test_server_with_protocol_backups_is_not_deleted() -> None:
    instance, protocol, fake = installed(CASES["xray"])
    protocol.save(instance, fake.host())

    with pytest.raises(ProtectedError):
        instance.server.delete()

    instance.delete()
    instance.server.delete()
    assert not ProtocolBackup.objects.exists()
