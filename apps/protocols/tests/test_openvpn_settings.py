"""OpenVPN settings change from the panel: a new container with the old certificate authority."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.test import Client

from amnezia_core.openvpn import OPENVPN_CONFIG_PATH, OPENVPN_DIR, OpenVpnSettings
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey
from apps.keys.services.profile import OpenVpnKeyProfile
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupErrorCode
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.openvpn_installer import OpenVpnInstaller
from apps.protocols.services.openvpn_settings import OpenVpnPanelSettings
from apps.protocols.services.settings_change import start_settings_change
from apps.protocols.settings_forms import OpenVpnSettingsForm
from apps.protocols.tests.fake_openvpn_host import CA_CERT, PKI_FILES, FakeOpenVpnHost
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

SERVER_IP = "203.0.113.7"
POOL = f"{OPENVPN_DIR}/ipp.txt"
OnCommit = Callable[..., AbstractContextManager[Any]]


@pytest.fixture
def openvpn(monkeypatch: pytest.MonkeyPatch) -> FakeOpenVpnHost:
    """A server with OpenVPN installed as the panel installs it and one client address."""
    server = FakeOpenVpnHost()
    params = {"port": 1194, "logs": False, "server_host": SERVER_IP}
    context = OperationContext(OperationFactory())
    OpenVpnInstaller.from_params(server.host(), context, params).run()
    server.files[POOL] = b"client,10.8.0.2\n"
    server.container_scripts.clear()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return server


@pytest.fixture
def instance() -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=ServerFactory(host=SERVER_IP),
        container="amnezia-openvpn",
        port=1194,
        subnet="10.8.0.0/24",
        params={"transport": "udp", "cipher": "AES-256-GCM", "hash": "SHA512",
                "ncp_disable": "false", "tls_auth": "true"},
        origin=InstanceOrigin.INSTALLED,
    )  # fmt: skip


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def key(instance: ProtocolInstance, admin: User) -> AccessKey:
    ca = PKI_FILES[CA_CERT].decode()
    profile = OpenVpnKeyProfile.build(SERVER_IP, OpenVpnSettings(), ca, "client cert", "ta")
    return AccessKey.objects.create(
        user=admin,
        source=instance.source,
        name="Laptop",
        public_key="client-1",
        private_key="client key",
        profile=profile.to_json(),
    )


def change(instance: ProtocolInstance, admin: User, capture: OnCommit, **fields: Any) -> Operation:
    values = {**OpenVpnPanelSettings.of(instance).to_params(), **fields}
    settings = OpenVpnPanelSettings.from_params(values)
    with capture(execute=True):
        operation = start_settings_change(instance, settings, admin, reissue=False)
    operation.refresh_from_db()
    return operation


def server_conf(openvpn: FakeOpenVpnHost) -> str:
    return openvpn.files[OPENVPN_CONFIG_PATH].decode()


def test_new_port_and_transport_start_a_new_container_with_the_same_authority(
    openvpn: FakeOpenVpnHost,
    instance: ProtocolInstance,
    admin: User,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    operation = change(
        instance, admin, django_capture_on_commit_callbacks, port=1195, transport="tcp",
        cipher="AES-128-GCM", tls_auth=False,
    )  # fmt: skip

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert "1195:1195/tcp" in openvpn.run_arguments
    assert openvpn.openvpn_up and openvpn.previous is None
    assert all(openvpn.files[path] == data for path, data in PKI_FILES.items())
    conf = server_conf(openvpn)
    assert "port 1195\n" in conf and "proto tcp" in conf and "cipher AES-128-GCM\n" in conf
    assert "tls-auth" not in conf
    assert openvpn.files[POOL] == b"client,10.8.0.2\n"
    server = OpenVpnSettings.from_json(OpenVpnKeyProfile.from_json(
        AccessKey.objects.get(pk=key.pk).profile).server)  # fmt: skip
    assert (server.port, server.transport, server.tls_auth) == (1195, "tcp", False)
    instance.refresh_from_db()
    assert instance.port == 1195 and instance.params["tls_auth"] == "false"


def test_new_subnet_forgets_the_client_addresses_of_the_old_one(
    openvpn: FakeOpenVpnHost,
    instance: ProtocolInstance,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    operation = change(instance, admin, django_capture_on_commit_callbacks, subnet="10.9.0.0")

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert "server 10.9.0.0 255.255.255.0" in server_conf(openvpn)
    assert POOL not in openvpn.files
    assert "1194:1194/udp" in openvpn.run_arguments
    instance.refresh_from_db()
    assert instance.subnet == "10.9.0.0/24"


@pytest.mark.parametrize("failure", ["copy_fails", "openvpn_fails"])
def test_old_container_comes_back_when_the_new_one_fails(
    openvpn: FakeOpenVpnHost,
    instance: ProtocolInstance,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
    failure: str,
) -> None:
    before = dict(openvpn.files)
    if failure == "copy_fails":
        openvpn.copy_fails = True
    else:
        openvpn.openvpn_starts = False

    operation = change(instance, admin, django_capture_on_commit_callbacks, port=1195)

    assert operation.status == OperationStatus.FAILED
    if failure == "copy_fails":
        assert operation.error_code == SetupErrorCode.OPENVPN_PKI_NOT_COPIED
    assert openvpn.files == before
    assert openvpn.container_state == "running" and openvpn.previous is None
    instance.refresh_from_db()
    assert instance.port == 1194


def test_settings_page_of_openvpn_offers_the_fields_of_the_app(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    response = admin_client.get(f"/protocols/{instance.pk}/settings/")

    form = response.context["form"]
    assert {"transport", "port", "subnet", "auto_negotiate", "hash", "cipher", "tls_auth"} <= set(
        form.fields
    )
    assert form.initial["subnet"] == "10.8.0.0" and form.initial["auto_negotiate"]


def test_form_keeps_the_network_of_the_subnet_and_asks_to_confirm_a_new_port(
    instance: ProtocolInstance,
) -> None:
    data = {"transport": "udp", "port": 1194, "subnet": "10.9.0.77", "auto_negotiate": "on",
            "hash": "SHA512", "cipher": "AES-256-GCM", "tls_auth": "on"}  # fmt: skip

    form = OpenVpnSettingsForm(data, instance=instance)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["subnet"] == "10.9.0.0"
    assert not form.changes_server

    moved = OpenVpnSettingsForm({**data, "port": 1195}, instance=instance)
    assert not moved.is_valid()
    assert "confirm" in moved.errors


def test_extra_config_reaches_server_conf_and_the_profiles(
    openvpn: FakeOpenVpnHost,
    instance: ProtocolInstance,
    admin: User,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    extra = 'push "route 10.20.0.0 255.255.0.0"\nmax-clients 50'
    operation = change(
        instance, admin, django_capture_on_commit_callbacks, additional_server_config=extra,
        additional_client_config="mssfix 1300", block_outside_dns=True,
    )  # fmt: skip

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    conf = server_conf(openvpn)
    assert extra in conf
    assert OpenVpnSettings.from_server_config(conf).additional_server_config == extra
    profile = OpenVpnKeyProfile.from_json(AccessKey.objects.get(pk=key.pk).profile)
    assert profile.block_outside_dns
    client = profile.client("client-1", "client key")
    assert "mssfix 1300" in client.native_config()
    last_config = client.vpn_config()["containers"][0]["openvpn"]["last_config"]
    assert '"block_outside_dns":true' in last_config
    instance.refresh_from_db()
    current = OpenVpnPanelSettings.of(instance)
    assert current.block_outside_dns and current.server.additional_client_config == "mssfix 1300"


def test_client_options_change_only_the_profiles(
    openvpn: FakeOpenVpnHost,
    instance: ProtocolInstance,
    admin: User,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    before = server_conf(openvpn)
    openvpn.run_arguments = []

    operation = change(
        instance, admin, django_capture_on_commit_callbacks,
        additional_client_config="mssfix 1300", block_outside_dns=True,
    )  # fmt: skip

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert server_conf(openvpn) == before and not openvpn.run_arguments
    assert "mssfix 1300" in AccessKey.objects.get(pk=key.pk).profile


def test_form_refuses_shell_characters_in_the_server_config(instance: ProtocolInstance) -> None:
    data = {"transport": "udp", "port": 1194, "subnet": "10.8.0.0", "auto_negotiate": "on",
            "hash": "SHA512", "cipher": "AES-256-GCM", "tls_auth": "on"}  # fmt: skip

    for bad in ("up $(reboot)", "verb 3\nEOF\nverb 4", "up `id`"):
        form = OpenVpnSettingsForm({**data, "additional_server_config": bad}, instance=instance)
        assert not form.is_valid()
        assert "additional_server_config" in form.errors

    form = OpenVpnSettingsForm(
        {**data, "additional_client_config": "  mssfix 1300\r\n", "block_outside_dns": "on"},
        instance=instance,
    )
    assert not form.is_valid()
    assert form.cleaned_data["additional_client_config"] == "mssfix 1300"
    assert "confirm" in form.errors
