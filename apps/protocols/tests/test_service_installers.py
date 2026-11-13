"""AmneziaDNS, SFTP and the Tor site are installed and removed as the Amnezia app does it."""

import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from amnezia_core.services import DNS_FORWARD_RECORDS_PATH, SFTP_USER
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.container import upstream_file
from apps.protocols.services.installation import InstallRequest, install_choices, start_install
from apps.protocols.services.installer import installers
from apps.protocols.services.service_installers import (
    DnsInstaller,
    SftpInstaller,
    TorSiteInstaller,
)
from apps.protocols.services.service_removal import start_service_removal
from apps.protocols.tests.fake_service_host import (
    APP_RECORDS,
    ONION,
    PANEL_RECORDS,
    Container,
    FakeServiceHost,
)
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeServiceHost:
    fake = FakeServiceHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt")


@pytest.fixture
def install(
    django_capture_on_commit_callbacks: OnCommit, server: Server, admin: User
) -> Callable[..., Operation]:
    """Start the install operation and run it at once, as the worker would."""

    def runner(container: str, port: int | None = None) -> Operation:
        installer = installers.get(container)
        request = InstallRequest(installer, port or installer.default_port)
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


def test_install_page_offers_the_services_with_app_defaults(server: Server) -> None:
    offered = {choice.container: choice for choice in install_choices(server)}

    assert offered["amnezia-dns"].installer is DnsInstaller
    assert offered["amnezia-sftp"].ports == "222/tcp"
    assert offered["amnezia-dns"].ports == "—"
    assert offered["amnezia-torwebsite"].ports == "—"
    assert TorSiteInstaller.published_ports(80) == set()


def test_dns_runs_at_the_address_vpn_containers_use(
    host: FakeServiceHost, install: Callable[..., Operation]
) -> None:
    operation = install("amnezia-dns")

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.containers["amnezia-dns"].arguments == [
        "docker", "run", "-d", "--log-driver", "none", "--restart", "always",
        "--network", "amnezia-dns-net", "--ip=172.29.172.254",
        "--name", "amnezia-dns", "amnezia-dns",
    ]  # fmt: skip
    assert "FROM mvance/unbound" in host.host_files["/opt/amnezia/amnezia-dns/Dockerfile"].decode()
    service = ServiceInstance.objects.get()
    assert (service.container, service.port, service.origin) == (
        "amnezia-dns",
        53,
        InstanceOrigin.INSTALLED,
    )
    assert not service.dns_logs_unset


def test_dns_does_not_log_the_queries_of_clients() -> None:
    """forward-records.conf turns the logs off before its first clause, still under server:."""
    dockerfile = upstream_file("dns/Dockerfile")
    echoed = re.search(r'RUN echo "(.*?)" \| tee (\S+)', dockerfile, re.S)
    assert echoed and echoed[2].endswith("/forward-records.conf")
    lines = [line.strip() for line in echoed[1].replace("\\n\\\n", "\n").splitlines()]
    server_part = lines[: next(i for i, line in enumerate(lines) if line.endswith("zone:"))]
    options = dict(line.split(": ", 1) for line in server_part if ": " in line)

    assert options["verbosity"] == "0"
    for name in ("log-queries", "log-replies", "log-tag-queryreply", "log-servfail"):
        assert options[name] == "no", name
    assert "logfile" not in options


def test_sftp_gets_a_random_password_kept_encrypted_and_out_of_the_log(
    host: FakeServiceHost, install: Callable[..., Operation]
) -> None:
    operation = install("amnezia-sftp", 2222)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    password = host.sftp_password()
    assert len(password) == 16 and password.isalnum()
    assert host.containers["amnezia-sftp"].arguments == [
        "docker", "run", "-d", "--log-driver", "none", "--restart", "always",
        "-p", "2222:22/tcp", "--name", "amnezia-sftp", "amnezia-sftp",
        f"{SFTP_USER}:{password}:::upload",
    ]  # fmt: skip
    assert host.containers["amnezia-sftp"].networks == {"bridge"}
    service = ServiceInstance.objects.get()
    assert (service.port, service.login, service.secret) == (2222, SFTP_USER, password)
    assert password not in raw_secret(service)
    assert password not in log_of(operation)
    assert password not in str(operation.params)


def test_tor_site_gets_its_onion_address_out_of_the_log(
    host: FakeServiceHost, install: Callable[..., Operation]
) -> None:
    operation = install("amnezia-torwebsite")

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    service = ServiceInstance.objects.get()
    assert service.secret == ONION
    assert ONION not in raw_secret(service)
    assert ONION not in log_of(operation)
    assert "--log-driver" in host.containers["amnezia-torwebsite"].arguments


def test_tor_without_an_onion_address_fails_and_keeps_the_container(
    host: FakeServiceHost, install: Callable[..., Operation]
) -> None:
    host.tor_starts = False

    operation = install("amnezia-torwebsite")

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "tor_no_onion"
    assert "amnezia-torwebsite" in host.containers
    assert not ServiceInstance.objects.exists()


def test_sftp_set_up_in_the_app_keeps_its_account(
    host: FakeServiceHost, install: Callable[..., Operation]
) -> None:
    arguments = ["docker", "run", "-p", "222:22/tcp", "--name", "amnezia-sftp", "amnezia-sftp"]
    host.containers["amnezia-sftp"] = Container([*arguments, "sftp_user:FromTheApp1:::upload"])

    operation = install("amnezia-sftp")

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert host.sftp_password() == "FromTheApp1"
    assert ServiceInstance.objects.get().secret == "FromTheApp1"


def test_import_reads_the_services_of_the_app(
    host: FakeServiceHost, server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    from apps.protocols.services.importer import start_import  # noqa: PLC0415

    host.containers["amnezia-sftp"] = Container(
        [
            "docker",
            "run",
            "-p",
            "222:22/tcp",
            "--name",
            "amnezia-sftp",
            "amnezia-sftp",
            "u:p1:::upload",
        ]
    )
    host.containers["amnezia-torwebsite"] = Container(["docker", "run"])

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_import(server, None)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    services = {s.container: s for s in ServiceInstance.objects.all()}
    assert (services["amnezia-sftp"].login, services["amnezia-sftp"].secret) == ("u", "p1")
    assert services["amnezia-sftp"].origin == InstanceOrigin.IMPORTED
    assert services["amnezia-torwebsite"].secret == ONION
    assert "p1" not in log_of(operation).replace("amnezia-", "")


def test_card_shows_the_service_data_and_removal_forgets_the_service(
    host: FakeServiceHost,
    install: Callable[..., Operation],
    admin_client: Client,
    server: Server,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    install("amnezia-sftp")
    install("amnezia-torwebsite")
    sftp = ServiceInstance.objects.get(container="amnezia-sftp")

    card = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert SFTP_USER in card and ONION in card
    # The password is sent only on request, so the look is in the journal.
    assert host.sftp_password() not in card
    revealed = admin_client.post(f"/protocols/services/{sftp.pk}/password/").content.decode()
    assert host.sftp_password() in revealed

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(f"/protocols/services/{sftp.pk}/remove/")

    operation = Operation.objects.latest("pk")
    assert response.headers["Location"] == f"/operations/{operation.pk}/"
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert "amnezia-sftp" not in host.containers and "amnezia-sftp" not in host.images
    assert list(ServiceInstance.objects.values_list("container", flat=True)) == [
        "amnezia-torwebsite"
    ]
    server.refresh_from_db()
    assert "amnezia-sftp" not in {c["name"] for c in server.containers}
    assert AuditEvent.objects.filter(action="protocols.remove_service").exists()
    assert host.locks >= 1


def test_removal_of_a_service_already_gone_still_forgets_it(
    host: FakeServiceHost,
    server: Server,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    service = ServiceInstance.objects.create(
        server=server, container="amnezia-dns", port=53, origin=InstanceOrigin.IMPORTED
    )

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_service_removal(service, admin)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    assert not ServiceInstance.objects.exists()


def test_installed_service_is_not_offered_again(server: Server) -> None:
    ServiceInstance.objects.create(
        server=server, container="amnezia-dns", origin=InstanceOrigin.INSTALLED
    )

    offered = {choice.container: choice for choice in install_choices(server)}

    assert not offered["amnezia-dns"].available
    assert offered["amnezia-sftp"].available


def test_service_removal_is_for_admins_only(client: Client, server: Server) -> None:
    service = ServiceInstance.objects.create(
        server=server, container="amnezia-dns", origin=InstanceOrigin.INSTALLED
    )
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.post(f"/protocols/services/{service.pk}/remove/").status_code == 403
    assert not Operation.objects.exists()


def test_sftp_installer_takes_only_port_and_log_from_the_form(server: Server) -> None:
    installer = SftpInstaller.from_params(
        FakeServiceHost().host(),
        None,  # type: ignore[arg-type]
        {"port": 222, "logs": True},
    )

    assert installer.logs and installer.port == 222
    assert "password" not in repr(installer.account)


DNS_RUN = [
    "docker", "run", "-d", "--log-driver", "none", "--restart", "always",
    "--network", "amnezia-dns-net", "--ip=172.29.172.254",
    "--name", "amnezia-dns", "amnezia-dns",
]  # fmt: skip


@pytest.fixture
def app_dns(
    host: FakeServiceHost, server: Server, django_capture_on_commit_callbacks: OnCommit
) -> ServiceInstance:
    """AmneziaDNS the app installed, read by the import."""
    from apps.protocols.services.importer import start_import  # noqa: PLC0415

    host.containers["amnezia-dns"] = Container(
        list(DNS_RUN),
        networks={"amnezia-dns-net"},
        files={DNS_FORWARD_RECORDS_PATH: APP_RECORDS},
    )
    with django_capture_on_commit_callbacks(execute=True):
        operation = start_import(server, None)
    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    return ServiceInstance.objects.get(container="amnezia-dns")


def rebuild(client: Client, service: ServiceInstance, on_commit: OnCommit) -> Operation:
    with on_commit(execute=True):
        response = client.post(f"/protocols/services/{service.pk}/rebuild-dns/")
    operation = Operation.objects.latest("pk")
    assert response.headers["Location"] == f"/operations/{operation.pk}/"
    operation.refresh_from_db()
    return operation


def test_dns_of_the_app_is_rebuilt_with_the_logs_off(
    host: FakeServiceHost,
    app_dns: ServiceInstance,
    admin_client: Client,
    server: Server,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    assert app_dns.dns_logs_unset
    assert "built by the app" in admin_client.get(f"/servers/{server.pk}/").content.decode()

    operation = rebuild(admin_client, app_dns, django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED, log_of(operation)
    dns = host.containers["amnezia-dns"]
    assert dns.arguments == DNS_RUN
    assert dns.files[DNS_FORWARD_RECORDS_PATH] == PANEL_RECORDS
    assert set(host.containers) == {"amnezia-dns"}
    assert "FROM mvance/unbound" in host.host_files["/opt/amnezia/amnezia-dns/Dockerfile"].decode()
    app_dns.refresh_from_db()
    assert not app_dns.dns_logs_unset
    assert app_dns.origin == InstanceOrigin.IMPORTED
    assert AuditEvent.objects.filter(action="protocols.rebuild_dns").exists()
    assert host.locks >= 1


def test_dns_that_does_not_start_after_the_rebuild_comes_back(
    host: FakeServiceHost,
    app_dns: ServiceInstance,
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    host.fails_to_start = True

    operation = rebuild(admin_client, app_dns, django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.FAILED
    dns = host.containers["amnezia-dns"]
    assert (dns.state, dns.files[DNS_FORWARD_RECORDS_PATH]) == ("running", APP_RECORDS)
    assert set(host.containers) == {"amnezia-dns"}
    app_dns.refresh_from_db()
    assert app_dns.dns_logs_unset


def test_stopped_dns_is_not_rebuilt(
    host: FakeServiceHost,
    app_dns: ServiceInstance,
    admin_client: Client,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    host.containers["amnezia-dns"].state = "exited"

    operation = rebuild(admin_client, app_dns, django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.FAILED
    assert host.containers["amnezia-dns"].files[DNS_FORWARD_RECORDS_PATH] == APP_RECORDS


def test_dns_rebuild_is_for_admins_and_for_dns_only(client: Client, server: Server) -> None:
    dns = ServiceInstance.objects.create(
        server=server, container="amnezia-dns", origin=InstanceOrigin.IMPORTED
    )
    sftp = ServiceInstance.objects.create(
        server=server, container="amnezia-sftp", origin=InstanceOrigin.IMPORTED
    )
    client.force_login(UserFactory(role=Role.MODERATOR))
    assert client.post(f"/protocols/services/{dns.pk}/rebuild-dns/").status_code == 403

    client.force_login(UserFactory(role=Role.ADMIN))
    assert client.post(f"/protocols/services/{sftp.pk}/rebuild-dns/").status_code == 404
    assert not Operation.objects.exists()
