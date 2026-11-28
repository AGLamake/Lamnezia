"""The cloud metadata block: the rule, closing and opening, install and import, the card."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from amnezia_core.containers import AWG2
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.operations.tasks import run_operation
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, MetadataBlock, ProtocolInstance
from apps.protocols.services import metadata_block
from apps.protocols.services import metadata_scripts as scripts
from apps.protocols.services.importer import IMPORT_KIND
from apps.protocols.services.installation import INSTALL_KIND
from apps.protocols.services.metadata_block import MetadataBlocker
from apps.protocols.services.metadata_scripts import MetadataReading
from apps.protocols.tests.fake_metadata_host import FakeMetadataHost
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

type OnCommit = Callable[..., Any]


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeMetadataHost:
    fake = FakeMetadataHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def admin_client(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def blocker(server: Server, host: FakeMetadataHost) -> tuple[MetadataBlocker, Operation]:
    operation = OperationFactory()
    return MetadataBlocker(server, host.host(), OperationContext(operation)), operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def awg_instance(server: Server) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=server,
        container=AWG2.name,
        port=55424,
        subnet="10.8.1.0/24",
        public_key="pub",
        private_key="priv",
        origin=InstanceOrigin.IMPORTED,
    )


# The rule and the status the script prints


def test_reading_keeps_the_timer_the_jump_and_the_bridges() -> None:
    reading = MetadataReading.parse(
        "timer active\njump yes\nclosed docker0\nclosed br-1a2b3c4d5e6f\nclosed bad;name\n"
        "closed6 docker0\nbackend nftables\nbackend other\n"
    )

    assert reading == MetadataReading(
        True, True, ("docker0", "br-1a2b3c4d5e6f"), ("docker0",), "nftables"
    )
    assert reading.closed


def test_rule_without_the_jump_or_the_timer_is_not_closed() -> None:
    assert not MetadataReading.parse("timer active\njump no\nclosed docker0\n").closed
    assert not MetadataReading.parse("timer inactive\njump yes\nclosed docker0\n").closed
    assert not MetadataReading.parse("timer active\njump yes\n").closed


def test_script_drops_link_local_from_the_amnezia_bridges_only() -> None:
    """Only FORWARD traffic of the containers: the host itself keeps the metadata service."""
    assert '"$1" -I DOCKER-USER -j LAMNEZIA-METADATA' in scripts.SCRIPT
    assert '-i "$dev" -d "$2" -j DROP' in scripts.SCRIPT
    assert "lamnezia_metadata_iptables iptables 169.254.0.0/16" in scripts.SCRIPT
    assert "hook forward" in scripts.SCRIPT
    assert "for net in bridge amnezia-dns-net" in scripts.SCRIPT
    assert "OUTPUT" not in scripts.SCRIPT
    assert "__" not in scripts.SCRIPT


def test_timer_starts_at_boot_and_repeats() -> None:
    assert "WantedBy=timers.target" in scripts.TIMER
    assert "OnBootSec=" in scripts.TIMER
    assert "OnUnitActiveSec=60s" in scripts.TIMER
    assert f"ExecStart=/bin/sh {scripts.SCRIPT_PATH}" in scripts.SERVICE
    assert "After=docker.service" in scripts.SERVICE


# Closing and opening


def test_enable_writes_units_and_closes_every_bridge(
    server: Server, host: FakeMetadataHost
) -> None:
    block, operation = blocker(server, host)

    reading = block.enable()

    assert host.files == {path: content.encode() for path, content, _mode in scripts.FILES}
    assert host.timer_active
    assert host.jumped
    assert host.closed == ["docker0", "br-1a2b3c4d5e6f"]
    assert host.locks == 1
    assert reading.closed
    assert "docker0: 169.254.0.0/16 closed" in log_of(operation)
    saved = MetadataBlock.objects.get(server=server)
    assert saved.enabled
    assert not saved.turned_off
    assert saved.bridges == ["docker0", "br-1a2b3c4d5e6f"]


def test_rule_outlives_a_reboot(server: Server, host: FakeMetadataHost) -> None:
    blocker(server, host)[0].enable()

    host.reboot()
    assert not host.jumped
    host.tick()

    assert host.jumped
    assert host.closed == ["docker0", "br-1a2b3c4d5e6f"]


def test_enable_again_changes_nothing_new(server: Server, host: FakeMetadataHost) -> None:
    blocker(server, host)[0].enable()

    block, operation = blocker(server, host)
    block.enable()

    assert host.closed == ["docker0", "br-1a2b3c4d5e6f"]
    assert "added" not in log_of(operation)
    assert MetadataBlock.objects.count() == 1


def test_disable_removes_everything_and_is_remembered(
    server: Server, host: FakeMetadataHost
) -> None:
    blocker(server, host)[0].enable()

    reading = blocker(server, host)[0].disable()

    assert host.files == {}
    assert not host.timer_active
    assert not host.jumped
    assert not reading.closed
    saved = MetadataBlock.objects.get(server=server)
    assert not saved.enabled
    assert saved.turned_off


def test_enable_after_disable_clears_the_choice(server: Server, host: FakeMetadataHost) -> None:
    blocker(server, host)[0].disable()

    blocker(server, host)[0].enable()

    assert not MetadataBlock.objects.get(server=server).turned_off


def test_server_without_systemd_is_refused_before_any_change(server: Server) -> None:
    host = FakeMetadataHost(systemd=False)

    with pytest.raises(SetupError) as caught:
        blocker(server, host)[0].enable()

    assert caught.value.code == SetupErrorCode.METADATA_NO_SYSTEMD
    assert host.files == {}


def test_check_changes_nothing_and_saves_the_reading(
    server: Server, host: FakeMetadataHost
) -> None:
    block, operation = blocker(server, host)

    reading = block.check()

    assert not reading.closed
    assert host.locks == 0
    assert host.files == {}
    assert "open to the VPN clients" in log_of(operation)
    assert not MetadataBlock.objects.get(server=server).enabled


# Install and import close the server on their own


def test_ensure_closes_a_server_with_a_protocol(server: Server, host: FakeMetadataHost) -> None:
    awg_instance(server)

    blocker(server, host)[0].ensure()

    assert host.jumped
    assert MetadataBlock.objects.get(server=server).enabled


def test_ensure_leaves_a_server_without_containers(server: Server, host: FakeMetadataHost) -> None:
    blocker(server, host)[0].ensure()

    assert host.files == {}
    assert not MetadataBlock.objects.exists()


def test_ensure_respects_the_administrator(server: Server, host: FakeMetadataHost) -> None:
    awg_instance(server)
    MetadataBlock.objects.create(server=server, turned_off=True)

    blocker(server, host)[0].ensure()

    assert host.files == {}
    assert host.locks == 0


def test_ensure_does_not_fail_without_systemd(server: Server) -> None:
    awg_instance(server)
    host = FakeMetadataHost(systemd=False)

    block, operation = blocker(server, host)
    block.ensure()

    assert "The server has no systemd" in log_of(operation)
    assert host.files == {}
    titles = list(operation.steps.values_list("title", flat=True))
    assert titles[-1] == "Leave the cloud metadata service open"


# Servers added before the block, or closed by an older script


def ensure_operations() -> list[Operation]:
    return list(Operation.objects.filter(kind=metadata_block.ENSURE_KIND))


def test_closing_keeps_the_version_of_the_script(server: Server, host: FakeMetadataHost) -> None:
    blocker(server, host)[0].enable()

    assert MetadataBlock.objects.get(server=server).script_version == scripts.VERSION


def test_catch_up_closes_an_old_server_once(server: Server) -> None:
    awg_instance(server)

    started = metadata_block.catch_up()

    assert [operation.params for operation in started] == [{"server_id": server.pk}]
    assert ensure_operations() == started
    assert MetadataBlock.objects.get(server=server).script_version == scripts.VERSION
    assert metadata_block.catch_up() == []


def test_catch_up_brings_the_new_script_to_a_closed_server(server: Server) -> None:
    awg_instance(server)
    MetadataBlock.objects.create(server=server, enabled=True, script_version=scripts.VERSION - 1)

    assert len(metadata_block.catch_up()) == 1


def test_catch_up_leaves_opened_and_empty_servers(server: Server) -> None:
    awg_instance(server)
    MetadataBlock.objects.create(server=server, turned_off=True)
    ServerFactory(name="Empty")

    assert metadata_block.catch_up() == []
    assert not MetadataBlock.objects.filter(script_version=scripts.VERSION).exists()


def heard(server: Server, *, answered: bool) -> None:
    """The latest monitor reading of the server: answered or not."""
    sample = ServerSample.objects.create(
        server=server, at=timezone.now(), error="" if answered else "host_unreachable"
    )
    seen = sample.at if answered else None
    Server.objects.filter(pk=server.pk).update(latest_sample=sample, last_seen_at=seen)


def test_catch_up_waits_for_a_silent_server_to_answer(server: Server) -> None:
    awg_instance(server)
    heard(server, answered=False)

    assert metadata_block.catch_up() == []
    assert not MetadataBlock.objects.filter(script_version=scripts.VERSION).exists()

    heard(server, answered=True)
    assert len(metadata_block.catch_up()) == 1


def test_a_server_the_operation_could_not_reach_is_caught_up_again(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    awg_instance(server)

    @contextmanager
    def unreachable(_server: Server) -> Iterator[RemoteHost]:
        raise SshConnectionError(ErrorCode.HOST_UNREACHABLE, server.host)
        yield  # pragma: no cover

    monkeypatch.setattr("apps.protocols.operations.connect", unreachable)
    [operation] = metadata_block.catch_up()

    run_operation(operation.pk)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert len(metadata_block.catch_up()) == 1


def test_catch_up_runs_every_hour() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE["protocols-metadata-catch-up"]
    assert schedule == {"task": "protocols.metadata_catch_up", "schedule": 60 * 60}


class _Importer:
    """Stands for the installer and the import: they leave a protocol in the panel."""

    def __init__(self, server: Server) -> None:
        self._server = server

    def run(self) -> None:
        awg_instance(self._server)


class _Installer:
    @classmethod
    def from_params(cls, *_args: Any) -> "_Installer":
        return cls()

    def run(self) -> None:
        pass


class _Installers:
    def get(self, _container: str) -> type[_Installer]:
        return _Installer


@pytest.mark.parametrize("kind", [INSTALL_KIND, IMPORT_KIND])
def test_install_and_import_close_the_server(  # noqa: PLR0913
    *,
    django_capture_on_commit_callbacks: OnCommit,
    monkeypatch: pytest.MonkeyPatch,
    admin: User,
    server: Server,
    host: FakeMetadataHost,
    kind: str,
) -> None:
    monkeypatch.setattr("apps.protocols.operations.installers", _Installers())
    monkeypatch.setattr(
        "apps.protocols.operations.ServerImporter", lambda srv, *_a, **_k: _Importer(srv)
    )
    params = {"server_id": server.pk, "container": AWG2.name, "port": 1, "logs": False}

    with django_capture_on_commit_callbacks(execute=True):
        operation = launch(kind, params, user=admin, subject=server.name)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.timer_active
    assert MetadataBlock.objects.get(server=server).enabled
    assert all(done.status == OperationStatus.SUCCEEDED for done in Operation.objects.all())


def test_import_itself_changes_nothing_on_the_server(
    monkeypatch: pytest.MonkeyPatch, admin: User, server: Server, host: FakeMetadataHost
) -> None:
    """The import only reads; closing the server is an operation of its own."""
    monkeypatch.setattr(
        "apps.protocols.operations.ServerImporter", lambda srv, *_a, **_k: _Importer(srv)
    )

    # Without a commit in the test the follow-up stays queued, as it is while the import ends.
    operation = launch(IMPORT_KIND, {"server_id": server.pk}, user=admin)
    run_operation(operation.pk)

    assert host.files == {}
    assert host.locks == 0
    follow_up = Operation.objects.get(kind=metadata_block.ENSURE_KIND)
    assert follow_up.status == OperationStatus.PENDING
    assert follow_up.created_by == admin


def test_import_of_an_opened_server_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, admin: User, server: Server, host: FakeMetadataHost
) -> None:
    MetadataBlock.objects.create(server=server, turned_off=True)
    monkeypatch.setattr(
        "apps.protocols.operations.ServerImporter", lambda srv, *_a, **_k: _Importer(srv)
    )

    operation = launch(IMPORT_KIND, {"server_id": server.pk}, user=admin)
    run_operation(operation.pk)

    assert not Operation.objects.filter(kind=metadata_block.ENSURE_KIND).exists()


# The server card


def test_card_buttons_start_the_operations(
    django_capture_on_commit_callbacks: OnCommit,
    admin_client: Client,
    server: Server,
    host: FakeMetadataHost,
) -> None:
    awg_instance(server)

    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "Cloud metadata service for VPN clients" in page

    for action in ("enable", "check", "disable"):
        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(f"/protocols/metadata/{server.pk}/{action}/")
        assert response.status_code == 302

    operations = list(Operation.objects.order_by("pk"))
    assert [operation.kind for operation in operations] == [
        metadata_block.ENABLE_KIND,
        metadata_block.CHECK_KIND,
        metadata_block.DISABLE_KIND,
    ]
    assert all(operation.status == OperationStatus.SUCCEEDED for operation in operations)
    assert list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)) == [
        metadata_block.ENABLE_KIND,
        metadata_block.DISABLE_KIND,
    ]
    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "opened by an administrator" in page


def test_card_shows_the_reading(admin_client: Client, server: Server) -> None:
    awg_instance(server)
    metadata_block.record(server, MetadataReading(True, True, ("docker0",), (), "iptables"))

    page = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "Closed for: docker0." in page
    assert "IPv6 is not closed" in page
    assert "Rules in iptables." in page
    assert f"/protocols/metadata/{server.pk}/disable/" in page

    metadata_block.record(server, MetadataReading(True, True, ("docker0",), ("docker0",)))
    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "IPv6 closed for: docker0." in page


def test_card_is_hidden_without_containers(admin_client: Client, server: Server) -> None:
    page = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "Cloud metadata service" not in page


def test_unknown_action_is_not_found(admin_client: Client, server: Server) -> None:
    response = admin_client.post(f"/protocols/metadata/{server.pk}/reboot/")

    assert response.status_code == 404
    assert not Operation.objects.exists()


def test_clients_cannot_touch_the_block(client: Client, server: Server) -> None:
    client.force_login(UserFactory())

    response = client.post(f"/protocols/metadata/{server.pk}/disable/")

    assert response.status_code in (302, 403)
    assert not Operation.objects.exists()
