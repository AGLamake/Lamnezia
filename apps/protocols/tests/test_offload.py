"""The TSO/GSO workaround: reading the offloads, turning it on and off, the card and the form."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.test import Client
from django.utils import translation

from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD
from amnezia_core.script_vars import TOGGLES
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.forms import ProtocolSettingsForm
from apps.protocols.install_form import InstallForm
from apps.protocols.models import InstanceOrigin, OffloadWorkaround, ProtocolInstance
from apps.protocols.services import offload
from apps.protocols.services import offload_scripts as scripts
from apps.protocols.services.installation import INSTALL_KIND, install_choices
from apps.protocols.services.offload import OffloadFix
from apps.protocols.services.offload_scripts import InterfaceOffload, OffloadReading
from apps.protocols.tests.fake_offload_host import FakeOffloadHost, Interface
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

type OnCommit = Callable[..., Any]


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeOffloadHost:
    fake = FakeOffloadHost()

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


def fix(server: Server, host: FakeOffloadHost) -> tuple[OffloadFix, Operation]:
    operation = OperationFactory()
    return OffloadFix(server, host.host(), OperationContext(operation)), operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def awg_instance(server: Server, container: str = AWG2.name) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=server,
        container=container,
        port=55424,
        subnet="10.8.1.0/24",
        public_key="pub",
        private_key="priv",
        origin=InstanceOrigin.IMPORTED,
    )


# The status the script prints


def test_reading_keeps_the_interfaces_and_the_timer() -> None:
    reading = OffloadReading.parse(
        "timer active\n"
        "amnezia-awg2 eth1 on off\n"
        "amnezia-wireguard eth0 off off\n"
        "something unexpected\n"
    )

    assert reading.timer_active
    assert reading.interfaces == (
        InterfaceOffload("amnezia-awg2", "eth1", "on", "off"),
        InterfaceOffload("amnezia-wireguard", "eth0", "off", "off"),
    )
    assert reading.glued == [reading.interfaces[0]]


def test_reading_without_ethtool_knows_nothing_of_the_offloads() -> None:
    reading = OffloadReading.parse("timer inactive\namnezia-awg eth1 - -\n")

    assert not reading.timer_active
    assert not reading.interfaces[0].known
    assert not reading.interfaces[0].glues


def test_interface_survives_json() -> None:
    interface = InterfaceOffload("amnezia-awg2", "eth1", "on", "on")

    assert InterfaceOffload.from_json(interface.to_json()) == interface


def test_script_covers_every_wireguard_family_container() -> None:
    for kind in (AWG2, AWG_LEGACY, WIREGUARD):
        assert kind.name in scripts.SCRIPT
    assert "{{.State.Pid}}" in scripts.SCRIPT


def test_timer_starts_at_boot_and_repeats() -> None:
    """Docker recreates the interface on a restart, the timer turns the offloads off again."""
    assert "WantedBy=timers.target" in scripts.TIMER
    assert "OnBootSec=" in scripts.TIMER
    assert "OnUnitActiveSec=30s" in scripts.TIMER
    assert f"ExecStart=/bin/sh {scripts.SCRIPT_PATH} apply" in scripts.SERVICE


# Turning the workaround on and off


def test_enable_installs_ethtool_writes_units_and_turns_the_offloads_off(
    server: Server, host: FakeOffloadHost
) -> None:
    workaround, operation = fix(server, host)

    reading = workaround.enable()

    assert host.installed == ["ethtool"]
    assert host.files == {path: content.encode() for path, content, _mode in scripts.FILES}
    assert host.timer_active
    assert host.interfaces["amnezia-awg2"] == Interface("eth1", "off", "off")
    assert host.locks == 1
    assert reading.timer_active
    assert "amnezia-awg2 eth1: TSO/GSO turned off" in log_of(operation)
    saved = OffloadWorkaround.objects.get(server=server)
    assert saved.enabled
    assert saved.interfaces == [
        {"container": "amnezia-awg2", "interface": "eth1", "tso": "off", "gso": "off"}
    ]


def test_workaround_outlives_a_container_restart(server: Server, host: FakeOffloadHost) -> None:
    fix(server, host)[0].enable()

    host.restart("amnezia-awg2")
    assert host.interfaces["amnezia-awg2"].tso == "on"
    host.tick()

    assert host.interfaces["amnezia-awg2"] == Interface("eth1", "off", "off")


def test_enable_again_changes_nothing_new(server: Server, host: FakeOffloadHost) -> None:
    fix(server, host)[0].enable()
    host.installed.clear()

    workaround, operation = fix(server, host)
    workaround.enable()

    assert host.installed == []
    assert "Everything is already installed." in log_of(operation)
    assert "turned off" not in log_of(operation)
    assert OffloadWorkaround.objects.count() == 1


def test_disable_removes_everything_and_turns_the_offloads_back_on(
    server: Server, host: FakeOffloadHost
) -> None:
    fix(server, host)[0].enable()

    reading = fix(server, host)[0].disable()

    assert host.files == {}
    assert not host.timer_active
    assert host.interfaces["amnezia-awg2"] == Interface("eth1", "on", "on")
    assert not reading.timer_active
    assert not OffloadWorkaround.objects.get(server=server).enabled
    host.restart("amnezia-awg2")
    host.tick()
    assert host.interfaces["amnezia-awg2"].tso == "on"


def test_server_without_systemd_is_refused_before_any_change(server: Server) -> None:
    host = FakeOffloadHost(systemd=False)

    with pytest.raises(SetupError) as caught:
        fix(server, host)[0].enable()

    assert caught.value.code == SetupErrorCode.NO_SYSTEMD
    assert host.files == {}
    assert host.installed == []


def test_failed_ethtool_install_is_reported(server: Server) -> None:
    host = FakeOffloadHost(apt_fails=True)

    with pytest.raises(SetupError) as caught:
        fix(server, host)[0].enable()

    assert caught.value.code == SetupErrorCode.OFFLOAD_TOOLS_FAILED
    assert host.files == {}


def test_check_changes_nothing_and_saves_the_reading(server: Server) -> None:
    host = FakeOffloadHost(commands={"ip", "nsenter", "ethtool"})

    workaround, operation = fix(server, host)
    reading = workaround.check()

    assert reading.glued == [InterfaceOffload("amnezia-awg2", "eth1", "on", "on")]
    assert host.locks == 0
    assert host.files == {}
    assert "amnezia-awg2 eth1: TSO on, GSO on" in log_of(operation)
    assert not OffloadWorkaround.objects.get(server=server).enabled


def test_check_without_ethtool_says_how_to_get_it(server: Server) -> None:
    host = FakeOffloadHost()

    workaround, operation = fix(server, host)
    workaround.check()

    assert "ethtool is not installed" in log_of(operation)


def test_state_reads_right_in_russian(server: Server, host: FakeOffloadHost) -> None:
    """The site catalog words a bare "on" for another noun: the state is a whole sentence."""
    with translation.override("ru"):
        workaround, operation = fix(server, host)
        workaround.check()
        workaround.enable()

        assert "Обход: выключен." in log_of(operation)
        assert "Обход: включён." in log_of(operation)
        form = ProtocolSettingsForm(instance=awg_instance(server))
        choices = dict(form.fields[next(iter(TOGGLES))].choices)  # type: ignore[attr-defined]
        assert (str(choices["on"]), str(choices["off"])) == ("вкл", "выкл")


# Operations, the server card and the install form


def test_card_buttons_start_the_operations(
    django_capture_on_commit_callbacks: OnCommit,
    admin_client: Client,
    server: Server,
    host: FakeOffloadHost,
) -> None:
    awg_instance(server)

    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "Upload speed through the VPN (TSO/GSO)" in page
    assert "workaround off" in page

    for action in ("enable", "check", "disable"):
        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(f"/protocols/offload/{server.pk}/{action}/")
        assert response.status_code == 302

    operations = list(Operation.objects.order_by("pk"))
    assert [operation.kind for operation in operations] == [
        offload.ENABLE_KIND,
        offload.CHECK_KIND,
        offload.DISABLE_KIND,
    ]
    assert all(operation.status == OperationStatus.SUCCEEDED for operation in operations)
    assert list(AuditEvent.objects.order_by("pk").values_list("action", flat=True)) == [
        offload.ENABLE_KIND,
        offload.DISABLE_KIND,
    ]
    assert host.files == {}


def test_card_shows_the_reading(admin_client: Client, server: Server) -> None:
    awg_instance(server)
    offload.record(
        server,
        OffloadReading(True, (InterfaceOffload("amnezia-awg2", "eth1", "off", "off"),)),
    )

    page = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "workaround on" in page
    assert "<td>eth1</td>" in page
    assert f"/protocols/offload/{server.pk}/disable/" in page


def test_card_is_hidden_without_a_wireguard_family_protocol(
    admin_client: Client, server: Server
) -> None:
    page = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "TSO/GSO" not in page


def test_unknown_action_is_not_found(admin_client: Client, server: Server) -> None:
    response = admin_client.post(f"/protocols/offload/{server.pk}/reboot/")

    assert response.status_code == 404
    assert not Operation.objects.exists()


def test_clients_cannot_touch_the_workaround(client: Client, server: Server) -> None:
    client.force_login(UserFactory())

    response = client.post(f"/protocols/offload/{server.pk}/enable/")

    assert response.status_code in (302, 403)
    assert not Operation.objects.exists()


def install_form(server: Server, container: str, **data: str) -> InstallForm:
    return InstallForm(
        {"container": container, "port": "", **data},
        server=server,
        choices=install_choices(server),
    )


def test_install_form_recommends_the_workaround(server: Server) -> None:
    form = InstallForm(server=server, choices=install_choices(server))

    assert form["offload"].initial is True
    assert "glues TCP packets" in str(form["offload"].help_text)


def test_workaround_is_asked_for_wireguard_family_installs_only(server: Server) -> None:
    form = install_form(server, AWG2.name, offload="on")
    assert form.is_valid(), form.errors
    assert form.request.offload

    form = install_form(server, AWG2.name)
    assert form.is_valid(), form.errors
    assert not form.request.offload

    form = install_form(server, "amnezia-xray", offload="on")
    assert form.is_valid(), form.errors
    assert not form.request.offload


class _Installer:
    """Stands for an installer and the import: the operation runs the workaround after them."""

    @classmethod
    def from_params(cls, *_args: Any) -> "_Installer":
        return cls()

    def run(self) -> None:
        pass


class _Installers:
    def get(self, _container: str) -> type[_Installer]:
        return _Installer


@pytest.mark.parametrize(
    ("container", "asked", "enabled"),
    [(AWG2.name, True, True), (AWG2.name, False, False), ("amnezia-xray", True, False)],
)
def test_install_turns_the_workaround_on_when_asked(  # noqa: PLR0913
    *,
    django_capture_on_commit_callbacks: OnCommit,
    monkeypatch: pytest.MonkeyPatch,
    admin: User,
    server: Server,
    host: FakeOffloadHost,
    container: str,
    asked: bool,
    enabled: bool,
) -> None:
    monkeypatch.setattr("apps.protocols.operations.installers", _Installers())
    monkeypatch.setattr("apps.protocols.operations.ServerImporter", lambda *_a, **_k: _Installer())
    params = {"server_id": server.pk, "container": container, "port": 1, "logs": False}
    if asked:
        params["offload"] = True

    with django_capture_on_commit_callbacks(execute=True):
        operation = launch(INSTALL_KIND, params, user=admin, subject=server.name)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.timer_active is enabled
