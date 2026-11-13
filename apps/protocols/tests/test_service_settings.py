"""The proxies get a new port and settings from the panel; the services without settings say why."""

import json
import socket
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from io import StringIO
from typing import Any

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import Client

from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    SECRET_PATH,
    SOCKS5_CONFIG_PATH,
    SOCKS5_CONTAINER,
    TPROXY_CONTAINER,
    TPROXY_META_PATH,
)
from amnezia_core.proxy_options import TelegramProxyOptions
from amnezia_core.services import DNS_CONTAINER, SFTP_CONTAINER, TOR_CONTAINER
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import retry
from apps.protocols.models import InstanceOrigin, ProtocolInstance, ServiceInstance
from apps.protocols.service_settings_forms import MtProtoSettingsForm, Socks5SettingsForm
from apps.protocols.services.installation import InstallRequest, start_install
from apps.protocols.services.installer import START_SCRIPT, installers
from apps.protocols.tests.fake_proxy_host import SECRET, FakeProxyHost
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
SERVER_IP = "203.0.113.7"
TAG = "aabbccddeeff00112233445566778899"
FAKETLS = TelegramProxyOptions(
    mode="faketls", tls_domain="example.org", public_host="tg.example.net", tag=TAG
)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeProxyHost:
    fake = FakeProxyHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both proxy domains point at the server."""
    names = {"proxy.example.com", "tg.example.com"}

    def getaddrinfo(name: str, *_args: Any) -> list[tuple[Any, ...]]:
        if name not in names:
            raise socket.gaierror(name)
        return [(socket.AF_INET, 0, 0, "", (SERVER_IP, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt", host=SERVER_IP)


@pytest.fixture
def install(
    host: FakeProxyHost, django_capture_on_commit_callbacks: OnCommit, server: Server
) -> Callable[..., ServiceInstance]:
    """Install a proxy at once, as the worker would."""

    def runner(container: str, port: int | None = None, **request: Any) -> ServiceInstance:
        installer = installers.get(container)
        admin = UserFactory(role=Role.ADMIN)
        with django_capture_on_commit_callbacks(execute=True):
            start_install(
                server, InstallRequest(installer, port or installer.default_port, **request), admin
            )
        return ServiceInstance.objects.get(container=container)

    return runner


@pytest.fixture
def change(
    admin_client: Client, django_capture_on_commit_callbacks: OnCommit
) -> Callable[..., Operation]:
    """Post the settings page and run the operation it starts."""

    def runner(service: ServiceInstance, **data: Any) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(f"/protocols/services/{service.pk}/settings/", data)
        assert response.status_code == 302, response.context["form"].errors
        operation = Operation.objects.latest("pk")
        assert operation.kind == "protocols.change_service_settings"
        return operation

    return runner


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def test_socks5_gets_a_new_account_on_its_port_without_showing_the_password(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(SOCKS5_CONTAINER)

    operation = change(service, port=service.port, user="alice", password="Pass_word1")

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.restarted == [SOCKS5_CONTAINER]
    assert "users alice:CL:Pass_word1" in host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH]
    service.refresh_from_db()
    assert (service.login, service.secret) == ("alice", "Pass_word1")
    audit = AuditEvent.objects.get(action="protocols.change_service_settings")
    for text in (json.dumps(operation.params), json.dumps(audit.data), log_of(operation)):
        assert "Pass_word1" not in text


def test_socks5_settings_are_retried_after_the_encryption_key_is_rotated(
    host: FakeProxyHost,
    install: Callable[..., ServiceInstance],
    change: Callable[..., Operation],
    django_capture_on_commit_callbacks: OnCommit,
    settings: Any,
) -> None:
    service = install(SOCKS5_CONTAINER)
    operation = change(service, port=service.port, user="alice", password="Pass_word1")
    Operation.objects.filter(pk=operation.pk).update(status=OperationStatus.FAILED)
    new = Fernet.generate_key().decode()
    settings.ENCRYPTION_KEY = f"{new},{settings.ENCRYPTION_KEY}"
    call_command("rotate_encryption_key", stdout=StringIO())
    settings.ENCRYPTION_KEY = new  # the old key is gone
    host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH] = ""

    with django_capture_on_commit_callbacks(execute=True):
        again = retry(Operation.objects.get(pk=operation.pk))

    again.refresh_from_db()
    assert again.status == OperationStatus.SUCCEEDED, log_of(again)
    assert "users alice:CL:Pass_word1" in host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH]


def test_socks5_on_a_new_port_runs_a_new_container(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(SOCKS5_CONTAINER)

    operation = change(service, port=1081, user=service.login, password=service.secret)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert set(host.containers) == {SOCKS5_CONTAINER}
    assert "1081:1081/tcp" in host.containers[SOCKS5_CONTAINER].arguments
    service.refresh_from_db()
    assert service.port == 1081


def test_mtproxy_keeps_its_secret_with_new_settings(
    host: FakeProxyHost,
    install: Callable[..., ServiceInstance],
    change: Callable[..., Operation],
    changed_services: list[ServiceInstance],
) -> None:
    service = install(MTPROXY_CONTAINER, proxy=FAKETLS)

    operation = change(service, **{**service.settings, "port": 443, "tls_domain": "example.com"})

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.restarted == [MTPROXY_CONTAINER]
    assert "domain=example.com\n" in host.volumes["amnezia-mtproxy-data"]["/data/mtproxy-meta"]
    service.refresh_from_db()
    assert service.secret == SECRET
    assert service.settings["tag"] == TAG
    assert service.links
    assert service.links.tg.endswith(f"secret=ee{SECRET}{b'example.com'.hex()}")
    assert changed_services == [service]


def test_failed_move_brings_back_the_old_proxy_and_its_settings(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(MTPROXY_CONTAINER, proxy=FAKETLS)
    host.failing_configures = 1

    data = {**service.settings, "port": 8444, "tls_domain": "example.com"}
    operation = change(service, **data)

    assert operation.status == OperationStatus.FAILED
    assert set(host.containers) == {MTPROXY_CONTAINER}
    container = host.containers[MTPROXY_CONTAINER]
    assert container.state == "running" and "443:443/tcp" in container.arguments
    volume = host.volumes["amnezia-mtproxy-data"]
    assert "domain=example.org\n" in volume["/data/mtproxy-meta"]
    assert volume[SECRET_PATH].strip() == SECRET
    service.refresh_from_db()
    assert service.port == 443


def test_failed_configure_on_the_same_port_brings_back_the_old_files(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(MTPROXY_CONTAINER, proxy=FAKETLS)
    volume = host.volumes["amnezia-mtproxy-data"]
    before = dict(volume)
    start_script = host.files[MTPROXY_CONTAINER][START_SCRIPT]
    host.failing_configures = 1

    operation = change(service, **{**service.settings, "port": 443, "tls_domain": "example.com"})

    assert operation.status == OperationStatus.FAILED
    assert volume == before
    assert host.files[MTPROXY_CONTAINER][START_SCRIPT] == start_script
    assert host.restarted == [MTPROXY_CONTAINER]
    assert host.containers[MTPROXY_CONTAINER].state == "running"
    assert MTPROXY_CONTAINER not in host.archives
    assert "The service runs with the old settings again." in log_of(operation)
    service.refresh_from_db()
    assert service.settings["tls_domain"] == "example.org"


def test_socks5_keeps_its_account_when_the_restart_fails(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(SOCKS5_CONTAINER)
    config = host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH]
    host.failing_restarts = 1

    operation = change(service, port=service.port, user="alice", password="Pass_word1")

    assert operation.status == OperationStatus.FAILED
    assert host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH] == config
    assert host.restarted == [SOCKS5_CONTAINER]


def test_tproxy_moves_to_a_new_domain(
    host: FakeProxyHost, install: Callable[..., ServiceInstance], change: Callable[..., Operation]
) -> None:
    service = install(TPROXY_CONTAINER, 8443, domain="proxy.example.com", email="a@example.com")

    operation = change(service, port=8443, domain="TG.example.com")

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    meta = host.volumes["amnezia-tproxy-data"][TPROXY_META_PATH]
    assert "hostname=tg.example.com\nemail=a@example.com\n" in meta
    service.refresh_from_db()
    assert service.settings == {"hostname": "tg.example.com"}


def test_forms_check_as_the_app_does(
    install: Callable[..., ServiceInstance], server: Server
) -> None:
    socks5 = install(SOCKS5_CONTAINER)
    mtproxy = install(MTPROXY_CONTAINER, proxy=FAKETLS)

    taken = Socks5SettingsForm({"port": 443, "user": "a", "password": "b"}, service=socks5)
    colon = Socks5SettingsForm({"port": 38080, "user": "a:b", "password": "b-c"}, service=socks5)
    own = Socks5SettingsForm({"port": 38080, "user": "a", "password": "b"}, service=socks5)
    data = {**mtproxy.settings, "port": 443, "tag": "not-hex"}
    tag = MtProtoSettingsForm(data, service=mtproxy)

    assert "port" in taken.errors
    assert "user" in colon.errors and "password" in colon.errors
    assert own.is_valid(), own.errors
    assert "tag" in tag.errors


@pytest.mark.parametrize(
    ("container", "shown"),
    [
        (SFTP_CONTAINER, "sftp-user"),
        (DNS_CONTAINER, "172.29.172.254"),
        (TOR_CONTAINER, "abcdef.onion"),
    ],
)
def test_services_without_settings_show_what_the_app_shows(
    admin_client: Client, server: Server, container: str, shown: str
) -> None:
    service = ServiceInstance.objects.create(
        server=server,
        container=container,
        port=2222 if container == SFTP_CONTAINER else None,
        login="sftp-user",
        secret=shown if container != DNS_CONTAINER else "",
        origin=InstanceOrigin.INSTALLED,
    )
    url = f"/protocols/services/{service.pk}/settings/"

    page = admin_client.get(url)

    assert page.status_code == 200
    assert shown in page.content.decode()
    assert admin_client.post(url, {"port": 1}).status_code == 404


class TestSftpPassword:
    @pytest.fixture
    def sftp(self, server: Server) -> ServiceInstance:
        return ServiceInstance.objects.create(
            server=server,
            container=SFTP_CONTAINER,
            port=2222,
            login="sftp-user",
            secret="sftp-password",
            origin=InstanceOrigin.INSTALLED,
        )

    def test_the_pages_do_not_hold_the_password(
        self, admin_client: Client, server: Server, sftp: ServiceInstance
    ) -> None:
        for url in (f"/protocols/services/{sftp.pk}/settings/", f"/servers/{server.pk}/"):
            html = admin_client.get(url).content.decode()

            assert f"/protocols/services/{sftp.pk}/password/" in html
            assert "sftp-password" not in html

    def test_showing_the_password_is_written_to_the_journal(
        self, admin_client: Client, sftp: ServiceInstance
    ) -> None:
        response = admin_client.post(f"/protocols/services/{sftp.pk}/password/")

        assert response.status_code == 200
        assert "sftp-password" in response.content.decode()
        assert "no-store" in response["Cache-Control"]
        event = AuditEvent.objects.get()
        assert (event.action, event.target_id) == ("protocols.service_password_view", str(sftp.pk))
        assert "sftp-password" not in str(event.data)

    def test_only_admins_see_it(self, client: Client, sftp: ServiceInstance) -> None:
        client.force_login(UserFactory(role=Role.MODERATOR))

        assert client.post(f"/protocols/services/{sftp.pk}/password/").status_code == 403
        assert not AuditEvent.objects.exists()

    def test_other_services_have_no_password_to_show(
        self, admin_client: Client, server: Server
    ) -> None:
        tor = ServiceInstance.objects.create(
            server=server, container=TOR_CONTAINER, secret="abc.onion"
        )

        assert admin_client.post(f"/protocols/services/{tor.pk}/password/").status_code == 404


def test_ipsec_settings_page_explains_its_fixed_ports(admin_client: Client, server: Server) -> None:
    ipsec = ProtocolInstance.objects.create(
        server=server, container="amnezia-ipsec", port=500, origin=InstanceOrigin.INSTALLED
    )

    page = admin_client.get(f"/protocols/{ipsec.pk}/settings/")

    assert page.status_code == 200
    assert "500/udp, 4500/udp" in page.content.decode()


def test_only_an_admin_changes_the_settings(
    client: Client, install: Callable[..., ServiceInstance]
) -> None:
    service = install(SOCKS5_CONTAINER)
    client.force_login(UserFactory(role=Role.MODERATOR))

    response = client.get(f"/protocols/services/{service.pk}/settings/")

    assert response.status_code in (302, 403)
