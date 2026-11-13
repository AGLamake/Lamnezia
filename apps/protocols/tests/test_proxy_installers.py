"""SOCKS5, MTProxy, Telemt and TProxy are installed, read and removed as the Amnezia app does it."""

import socket
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    SECRET_PATH,
    SOCKS5_CONFIG_PATH,
    SOCKS5_CONTAINER,
    SOCKS5_LOG_PATH,
    SOCKS5_USER,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
)
from amnezia_core.proxy_options import TelegramProxyOptions
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.protocols.install_form import InstallForm
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import upstream_file
from apps.protocols.services.installation import InstallRequest, install_choices, start_install
from apps.protocols.services.installer import installers
from apps.protocols.services.tproxy_installer import TProxyInstaller
from apps.protocols.tests.fake_proxy_host import RENEWED_SECRET, SECRET, FakeProxyHost
from apps.protocols.tests.fake_service_host import Container
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
SERVER_IP = "203.0.113.7"


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeProxyHost:
    fake = FakeProxyHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """IPv4 addresses of the names the installers look up; proxy.example.com is the server."""
    names = {"proxy.example.com": [SERVER_IP]}

    def getaddrinfo(name: str, *_args: Any) -> list[tuple[Any, ...]]:
        if name not in names:
            raise socket.gaierror(name)
        return [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in names[name]]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return names


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt", host=SERVER_IP)


@pytest.fixture
def install(
    django_capture_on_commit_callbacks: OnCommit, server: Server, admin: User
) -> Callable[..., Operation]:
    """Start the install operation and run it at once, as the worker would."""

    def runner(
        container: str,
        port: int | None = None,
        *,
        domain: str = "",
        email: str = "",
        proxy: TelegramProxyOptions | None = None,
    ) -> Operation:
        installer = installers.get(container)
        request = InstallRequest(
            installer, port or installer.default_port, domain=domain, email=email, proxy=proxy
        )
        with django_capture_on_commit_callbacks(execute=True):
            operation = start_install(server, request, admin)
        operation.refresh_from_db()
        return operation

    return runner


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def raw_secret(service: ServiceInstance) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT secret FROM {ServiceInstance._meta.db_table} WHERE id = %s", [service.pk]
        )
        return str(cursor.fetchone()[0])


def test_install_page_offers_the_proxies_with_app_defaults(server: Server) -> None:
    offered = {choice.container: choice for choice in install_choices(server)}

    assert offered[SOCKS5_CONTAINER].ports == "38080/tcp"
    assert offered[MTPROXY_CONTAINER].ports == "443/tcp"
    assert offered[TELEMT_CONTAINER].ports == "443/tcp"
    assert TProxyInstaller.published_ports(8443) == {(8443, "tcp"), (80, "tcp")}


def test_socks5_gets_a_random_password_kept_encrypted_and_out_of_the_log(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(SOCKS5_CONTAINER, 1080)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.containers[SOCKS5_CONTAINER].arguments == [
        "docker", "run", "-d", "--log-driver", "none", "--restart", "always",
        "-p", "1080:1080/tcp", "--name", SOCKS5_CONTAINER, SOCKS5_CONTAINER,
    ]  # fmt: skip
    assert host.started == [SOCKS5_CONTAINER]
    assert "/bin/3proxy" in host.files[SOCKS5_CONTAINER]["/opt/amnezia/start.sh"]
    service = ServiceInstance.objects.get()
    assert (service.port, service.login) == (1080, SOCKS5_USER)
    assert len(service.secret) == 16 and service.secret.isalnum()
    assert (
        f"users {SOCKS5_USER}:CL:{service.secret}"
        in host.files[SOCKS5_CONTAINER][SOCKS5_CONFIG_PATH]
    )
    assert service.secret not in raw_secret(service)
    assert service.secret not in log_of(operation)
    assert service.secret not in str(operation.params)


def test_socks5_does_not_log_the_hosts_users_visit() -> None:
    script = upstream_file("socks5_proxy/configure_container.sh")

    commands = [line for line in script.splitlines() if not line.startswith("#")]
    assert not [line for line in commands if line.startswith(('echo -e "log', "log"))]
    assert "%n" not in script


def test_socks5_set_up_in_the_app_keeps_its_account(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    host.containers[SOCKS5_CONTAINER] = Container(["docker", "run", "-p", "38080:38080/tcp"])
    host.put(SOCKS5_CONTAINER, SOCKS5_CONFIG_PATH, "users app_user:CL:FromTheApp1\n")

    operation = install(SOCKS5_CONTAINER)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    service = ServiceInstance.objects.get()
    assert (service.login, service.secret) == ("app_user", "FromTheApp1")


APP_CONFIG = "users app_user:CL:FromTheApp1\nlog /usr/local/3proxy/logs/3proxy.log D\n"
VISITED = "07112026 example.org:443\n"


def _app_socks5(host: FakeProxyHost, config: str = APP_CONFIG) -> None:
    """amnezia-socks5proxy set up in the app: its 3proxy.cfg logs the hosts users visit."""
    host.containers[SOCKS5_CONTAINER] = Container(["docker", "run", "-p", "38080:38080/tcp"])
    host.put(SOCKS5_CONTAINER, SOCKS5_CONFIG_PATH, config)
    host.put(SOCKS5_CONTAINER, SOCKS5_LOG_PATH, VISITED)


def _import(server: Server, on_commit: OnCommit) -> Operation:
    from apps.protocols.services.importer import start_import  # noqa: PLC0415

    with on_commit(execute=True):
        operation = start_import(server, None)
    operation.refresh_from_db()
    return operation


def test_import_notes_the_log_the_app_left_in_socks5(
    host: FakeProxyHost,
    server: Server,
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    _app_socks5(host)

    operation = _import(server, django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert "3proxy.log" in log_of(operation)
    service = ServiceInstance.objects.get()
    assert service.app_log_size == len(VISITED)
    card = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert f"/protocols/services/{service.pk}/app-log/" in card


def test_install_over_the_proxy_of_the_app_notes_its_log(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    _app_socks5(host)

    operation = install(SOCKS5_CONTAINER)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert "3proxy.log" in log_of(operation)
    assert ServiceInstance.objects.get().app_log_size == len(VISITED)


def test_socks5_without_a_log_offers_nothing_to_delete(
    host: FakeProxyHost, install: Callable[..., Operation], admin_client: Client
) -> None:
    install(SOCKS5_CONTAINER)
    service = ServiceInstance.objects.get()

    assert service.app_log_size is None
    card = admin_client.get(f"/servers/{service.server_id}/").content.decode()
    assert "/app-log/" not in card


def test_deleting_the_log_turns_it_off_and_keeps_the_account(
    host: FakeProxyHost,
    server: Server,
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    _app_socks5(host)
    _import(server, django_capture_on_commit_callbacks)
    service = ServiceInstance.objects.get()

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(f"/protocols/services/{service.pk}/app-log/")

    operation = Operation.objects.latest("pk")
    assert response.headers["Location"] == f"/operations/{operation.pk}/"
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    files = host.files[SOCKS5_CONTAINER]
    assert SOCKS5_LOG_PATH not in files
    assert "users app_user:CL:FromTheApp1" in files[SOCKS5_CONFIG_PATH]
    assert "log " not in files[SOCKS5_CONFIG_PATH]
    assert host.restarted == [SOCKS5_CONTAINER]
    assert "FromTheApp1" not in log_of(operation)
    service = ServiceInstance.objects.get()
    assert service.app_log_size is None
    assert (service.login, service.secret) == ("app_user", "FromTheApp1")
    assert service.origin == InstanceOrigin.IMPORTED
    assert AuditEvent.objects.filter(action="protocols.delete_proxy_log").exists()


@pytest.mark.parametrize(
    ("config", "state", "code"),
    [
        ("auth none\nlog /usr/local/3proxy/logs/3proxy.log D\n", "running", "socks5_no_account"),
        (APP_CONFIG, "exited", "socks5_not_running"),
    ],
)
def test_log_is_kept_when_the_proxy_cannot_be_rewritten(  # noqa: PLR0913, PLR0917
    host: FakeProxyHost,
    server: Server,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
    config: str,
    state: str,
    code: str,
) -> None:
    from apps.protocols.services.proxy_log import start_log_deletion  # noqa: PLC0415

    _app_socks5(host, config)
    _import(server, django_capture_on_commit_callbacks)
    host.containers[SOCKS5_CONTAINER].state = state

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_log_deletion(ServiceInstance.objects.get(), admin)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == code
    assert host.files[SOCKS5_CONTAINER][SOCKS5_LOG_PATH] == VISITED
    assert not host.restarted


def test_only_socks5_has_a_log_to_delete(
    host: FakeProxyHost, install: Callable[..., Operation], admin_client: Client
) -> None:
    install(MTPROXY_CONTAINER)
    service = ServiceInstance.objects.get()

    assert admin_client.post(f"/protocols/services/{service.pk}/app-log/").status_code == 404
    assert not Operation.objects.filter(kind="protocols.delete_proxy_log").exists()


def test_mtproxy_gets_a_secret_and_a_telegram_link(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(MTPROXY_CONTAINER, 8443)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.containers[MTPROXY_CONTAINER].arguments == [
        "docker", "run", "-d", "--log-driver", "none", "--restart", "always",
        "-p", "8443:8443/tcp", "-v", "amnezia-mtproxy-data:/data",
        "--name", MTPROXY_CONTAINER, MTPROXY_CONTAINER,
    ]  # fmt: skip
    service = ServiceInstance.objects.get()
    assert service.secret == SECRET
    assert service.settings["mode"] == "standard"
    assert service.links
    assert service.links.tg == f"tg://proxy?server={SERVER_IP}&port=8443&secret=dd{SECRET}"
    assert service.links.tme.startswith("https://t.me/proxy?server=")
    assert SECRET not in raw_secret(service)
    assert SECRET not in log_of(operation)


def test_telemt_runs_with_the_file_limit_of_the_app(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(TELEMT_CONTAINER)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    arguments = host.containers[TELEMT_CONTAINER].arguments
    assert arguments[7:13] == [
        "--ulimit", "nofile=65536:65536", "-p", "443:443/tcp", "-v", "amnezia-telemt-data:/data",
    ]  # fmt: skip
    service = ServiceInstance.objects.get()
    assert (service.port, service.login, service.secret) == (443, "amnezia", SECRET)
    assert service.links
    assert service.links.tg.endswith(f"&port=443&secret=dd{SECRET}")


def test_tproxy_publishes_http_for_the_certificate_and_links_its_domain(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(
        TPROXY_CONTAINER, 8443, domain="proxy.example.com", email="admin@example.com"
    )

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    arguments = host.containers[TPROXY_CONTAINER].arguments
    assert arguments[7:13] == [
        "-p", "80:80/tcp", "-p", "8443:443/tcp", "-v", "amnezia-tproxy-data:/data",
    ]  # fmt: skip
    assert f'EXTERNAL_IP="{SERVER_IP}"' in host.files[TPROXY_CONTAINER]["/opt/amnezia/start.sh"]
    service = ServiceInstance.objects.get()
    assert service.port == 8443
    assert service.settings == {"hostname": "proxy.example.com"}
    assert service.links
    assert service.links.tg == f"tg://webproxy?server=proxy.example.com&secret={SECRET}"


def test_reinstall_keeps_the_secret_of_the_volume(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    host.volumes["amnezia-mtproxy-data"] = {SECRET_PATH: "ffffffffffffffffffffffffffffffff\n"}

    operation = install(MTPROXY_CONTAINER)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert ServiceInstance.objects.get().secret == "f" * 32


def test_telegram_proxy_without_a_secret_fails_and_keeps_the_container(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    host.online = False

    operation = install(MTPROXY_CONTAINER)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "proxy_configure_failed"
    assert MTPROXY_CONTAINER in host.containers
    assert not ServiceInstance.objects.exists()


def test_import_reads_the_proxies_of_the_app(
    host: FakeProxyHost, server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    from apps.protocols.services.importer import start_import  # noqa: PLC0415

    host.containers[TPROXY_CONTAINER] = Container(
        ["docker", "run", "-p", "80:80/tcp", "-p", "443:443/tcp"]
    )
    host.put(TPROXY_CONTAINER, SECRET_PATH, f"{SECRET}\n")
    host.put(TPROXY_CONTAINER, "/data/tproxy-meta", "hostname=t.example.com\nhttps_port=443\n")
    host.containers[MTPROXY_CONTAINER] = Container(["docker", "run", "-p", "993:993/tcp"])
    host.put(MTPROXY_CONTAINER, SECRET_PATH, f"{SECRET}\n")
    host.put(MTPROXY_CONTAINER, "/data/mtproxy-meta", "mode=faketls\ndomain=example.org\n")
    host.containers[SOCKS5_CONTAINER] = Container(["docker", "run", "-p", "38080:38080/tcp"])

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_import(server, None)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    services = {s.container: s for s in ServiceInstance.objects.all()}
    assert services[TPROXY_CONTAINER].port == 443
    assert services[TPROXY_CONTAINER].origin == InstanceOrigin.IMPORTED
    mtproxy = services[MTPROXY_CONTAINER].links
    assert mtproxy
    assert mtproxy.tg.endswith(f"&port=993&secret=ee{SECRET}{b'example.org'.hex()}")
    assert (services[SOCKS5_CONTAINER].login, services[SOCKS5_CONTAINER].secret) == ("", "")
    assert SECRET not in log_of(operation)


def test_card_shows_the_links_and_removal_takes_the_volume(
    host: FakeProxyHost,
    install: Callable[..., Operation],
    admin_client: Client,
    server: Server,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    install(MTPROXY_CONTAINER)
    install(SOCKS5_CONTAINER)
    mtproxy = ServiceInstance.objects.get(container=MTPROXY_CONTAINER)
    socks5 = ServiceInstance.objects.get(container=SOCKS5_CONTAINER)

    card = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert f"secret=dd{SECRET}" in card
    assert f"{SERVER_IP}:38080" in card and socks5.secret in card

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/protocols/services/{mtproxy.pk}/remove/")

    operation = Operation.objects.latest("pk")
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert MTPROXY_CONTAINER not in host.containers
    assert "amnezia-mtproxy-data" not in host.volumes


def _form(server: Server, **data: str) -> InstallForm:
    return InstallForm(
        {"container": TPROXY_CONTAINER, "port": "", **data},
        server=server,
        choices=install_choices(server),
    )


def test_tproxy_form_asks_for_a_domain_and_an_email(server: Server) -> None:
    form = _form(server, domain=SERVER_IP, email="")

    assert not form.is_valid()
    assert set(form.errors) == {"domain", "email"}

    form = _form(server, domain="Proxy.Example.com ", email="admin@example.com")

    assert form.is_valid(), form.errors
    request = form.request
    assert (request.port, request.domain, request.email) == (
        443,
        "proxy.example.com",
        "admin@example.com",
    )


TAG = "aabbccddeeff00112233445566778899"
EXTRA = "99887766554433221100ffeeddccbbaa"
FAKETLS = TelegramProxyOptions(
    mode="faketls",
    tls_domain="example.org",
    public_host="tg.example.net",
    tag=TAG,
    additional_secrets=(EXTRA,),
    nat_internal_ip="10.0.0.5",
    nat_external_ip=SERVER_IP,
)


def test_mtproxy_keeps_its_settings_where_the_app_reads_them(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(MTPROXY_CONTAINER, proxy=FAKETLS)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    meta = host.volumes["amnezia-mtproxy-data"]["/data/mtproxy-meta"]
    assert "mode=faketls\ndomain=example.org\n" in meta
    assert f"tag={TAG}\nadditional={EXTRA}\nworkers_mode=auto\nworkers=0\n" in meta
    assert f"nat_enabled=1\nnat_internal=10.0.0.5\nnat_external={SERVER_IP}\n" in meta
    assert "public_host=tg.example.net" in meta
    start = host.files[MTPROXY_CONTAINER]["/opt/amnezia/start.sh"]
    assert f'TAG_ARG="-P {TAG}"' in start and "--domain example.org" in start
    links = ServiceInstance.objects.get().links
    assert links
    domain = b"example.org".hex()
    assert links.tg == f"tg://proxy?server=tg.example.net&port=443&secret=ee{SECRET}{domain}"


def test_telemt_writes_the_settings_into_its_config(
    host: FakeProxyHost, install: Callable[..., Operation]
) -> None:
    operation = install(TELEMT_CONTAINER, proxy=FAKETLS)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    config = host.volumes["amnezia-telemt-data"]["/data/config.toml"]
    assert f'ad_tag = "{TAG}"\nmiddle_proxy_nat_ip = "{SERVER_IP}"' in config
    assert "secure = false\ntls = true" in config
    assert f'extra_1 = "{EXTRA}"' in config
    links = ServiceInstance.objects.get().links
    assert links
    assert links.tg.startswith("tg://proxy?server=tg.example.net&port=443&secret=ee")


def _proxy_form(server: Server, container: str, **data: str) -> InstallForm:
    return InstallForm(
        {"container": container, "port": "", **data}, server=server, choices=install_choices(server)
    )


def test_proxy_form_checks_what_the_proxy_would_reject(server: Server) -> None:
    form = _proxy_form(
        server,
        MTPROXY_CONTAINER,
        mode="faketls",
        tls_domain="203.0.113.9",
        public_host="not a host",
        tag="xyz",
        additional_secrets=f"{EXTRA}, 1234",
        nat_external_ip=SERVER_IP,
    )

    assert not form.is_valid()
    assert set(form.errors) == {
        "tls_domain", "public_host", "tag", "additional_secrets", "nat_internal_ip",
    }  # fmt: skip


def test_proxy_form_gives_each_installer_only_its_fields(server: Server) -> None:
    data = {
        "mode": "faketls",
        "tls_domain": "Example.org",
        "tag": TAG.upper(),
        "additional_secrets": f"{EXTRA} {TAG}",
        "nat_internal_ip": "10.0.0.5",
        "nat_external_ip": SERVER_IP,
        "site": "www.example.com",
    }
    telemt = _proxy_form(server, TELEMT_CONTAINER, **data)
    socks5 = _proxy_form(server, SOCKS5_CONTAINER, **data)

    assert telemt.is_valid(), telemt.errors
    assert telemt.request.proxy == TelegramProxyOptions(
        mode="faketls",
        tls_domain="example.org",
        tag=TAG,
        additional_secrets=(EXTRA, TAG),
        nat_external_ip=SERVER_IP,
    )
    assert telemt.request.site == ""
    assert socks5.is_valid(), socks5.errors
    assert socks5.request.proxy is None
    shown = {field.name: condition for field, condition in telemt.installer_fields}
    assert TELEMT_CONTAINER in shown["tag"] and TPROXY_CONTAINER not in shown["tag"]
    assert TELEMT_CONTAINER not in shown["nat_internal_ip"]
    assert shown["domain"] == f"['{TPROXY_CONTAINER}'].includes(container)"


def test_new_secret_keeps_the_settings_and_changes_the_links(
    host: FakeProxyHost,
    install: Callable[..., Operation],
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
    changed_services: list[ServiceInstance],
) -> None:
    install(MTPROXY_CONTAINER, proxy=FAKETLS)
    service = ServiceInstance.objects.get()

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/protocols/services/{service.pk}/secret/")

    operation = Operation.objects.latest("pk")
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert RENEWED_SECRET not in log_of(operation)
    assert host.restarted == [MTPROXY_CONTAINER]
    renewal = host.scripts[MTPROXY_CONTAINER][-1]
    assert 'if [ "1" = "1" ]' in renewal and f'"{TAG}"' in renewal
    service.refresh_from_db()
    assert service.secret == RENEWED_SECRET
    assert service.links
    assert f"secret=ee{RENEWED_SECRET}" in service.links.tg
    assert "server=tg.example.net" in service.links.tg
    assert AuditEvent.objects.filter(action="protocols.renew_proxy_secret").exists()
    assert changed_services == [service]


def test_new_secret_of_telemt_keeps_its_switches(
    host: FakeProxyHost,
    install: Callable[..., Operation],
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    install(TELEMT_CONTAINER, 8443)
    volume = host.volumes["amnezia-telemt-data"]
    volume["/data/config.toml"] = volume["/data/config.toml"].replace("mask = true", "mask = false")
    service = ServiceInstance.objects.get()

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/protocols/services/{service.pk}/secret/")

    assert Operation.objects.latest("pk").status == OperationStatus.SUCCEEDED
    assert "mask = false" in volume["/data/config.toml"]
    assert "port = 8443" in volume["/data/config.toml"]
    service.refresh_from_db()
    assert service.secret == RENEWED_SECRET


def test_only_a_telegram_proxy_gets_a_new_secret(
    host: FakeProxyHost, install: Callable[..., Operation], admin_client: Client
) -> None:
    install(SOCKS5_CONTAINER)
    socks5 = ServiceInstance.objects.get()

    assert admin_client.post(f"/protocols/services/{socks5.pk}/secret/").status_code == 404
    card = admin_client.get(f"/servers/{socks5.server_id}/").content.decode()
    assert "/secret/" not in card


BUSY_80 = "caddy 1 root TCP *:80 (LISTEN)"


@pytest.mark.parametrize(
    ("names", "port_users", "code"),
    [
        ({"proxy.example.com": ["198.51.100.1"]}, "", "tproxy_domain_elsewhere"),
        ({}, "", "tproxy_domain_unresolved"),
        ({"proxy.example.com": [SERVER_IP]}, BUSY_80, "tproxy_http_port_busy"),
    ],
)
def test_tproxy_refuses_before_anything_changes(  # noqa: PLR0913, PLR0917
    host: FakeProxyHost,
    install: Callable[..., Operation],
    dns: dict[str, list[str]],
    names: dict[str, list[str]],
    port_users: str,
    code: str,
) -> None:
    dns.clear()
    dns.update(names)
    host.port_users = port_users

    operation = install(TPROXY_CONTAINER, domain="proxy.example.com", email="admin@example.com")

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == code
    assert TPROXY_CONTAINER not in host.containers
    assert not host.images
