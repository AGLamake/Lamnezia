"""No kind of operation shows a secret of its parameters in the audit, its steps or its page.

A kind that carries a secret under a key of Operation.SECRET_PARAMS gets a case here; the last
test fails while a key of SECRET_PARAMS has no case.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import pytest
from django.test import Client

from amnezia_core.keys import KeyPair
from amnezia_core.proxies import MTPROXY_CONTAINER, SOCKS5_CONTAINER
from amnezia_core.proxy_options import TelegramProxyOptions
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.backups.services.database_dump import PostgresDump
from apps.backups.services.panel_archives import PANEL_ARCHIVE_KIND, start_panel_archive
from apps.backups.tests.panel_archive_helpers import PASSWORD, FakeDump
from apps.groups.models import ServerGroup
from apps.groups.services.membership import SETTINGS_KIND as GROUP_SETTINGS_KIND
from apps.groups.services.membership import create_group
from apps.groups.services.settings import start_group_settings
from apps.groups.tests.conftest import LEADER_FILES, LEADER_HOST, fake_server, instance_of
from apps.operations.models import Operation, OperationStatus
from apps.operations.tests.secrets import secrets_written
from apps.protocols.models import ServiceInstance
from apps.protocols.services.awg_settings import SETTINGS_KIND, AwgSettings
from apps.protocols.services.installation import INSTALL_KIND, InstallRequest, start_install
from apps.protocols.services.installer import installers
from apps.protocols.services.service_settings import (
    SERVICE_SETTINGS_KIND,
    ServiceSettings,
    start_service_settings_change,
)
from apps.protocols.services.settings_change import start_settings_change
from apps.protocols.tests.fake_proxy_host import FakeProxyHost
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]


class FakeHost(Protocol):
    def host(self) -> RemoteHost: ...


@dataclass(frozen=True)
class Started:
    """A finished operation, the secret it was given and its secret params as launched."""

    operation: Operation
    secret: str
    tokens: dict[str, str]


@dataclass(frozen=True)
class World:
    """The panel a case runs in: an admin, fake servers and the worker."""

    admin: User
    monkeypatch: pytest.MonkeyPatch
    settings: Any
    tmp_path: Path
    on_commit: OnCommit

    def connect(self, fake: FakeHost, *modules: str) -> None:
        """Every server the operation connects to is `fake`."""

        @contextmanager
        def connect(_server: Server) -> Iterator[RemoteHost]:
            yield fake.host()

        for module in modules:
            self.monkeypatch.setattr(f"{module}.connect", connect)

    def run(self, launch: Callable[[], Operation], secret: str) -> Started:
        """Start the operation and run it at once, as the worker would."""
        with self.on_commit(execute=True):
            operation = launch()
        # The worker loads a copy of its own: this one keeps the params as launched.
        params = operation.params
        tokens = {key: params[key] for key in Operation.SECRET_PARAMS if key in params}
        operation.refresh_from_db()
        return Started(operation, secret, tokens)


def _new_header_protection_key(settings: AwgSettings) -> tuple[AwgSettings, str]:
    key = KeyPair.generate().private_key
    return replace(settings, params={**settings.params, "HeaderProtectionKey": key}), key


def awg_settings(world: World) -> Started:
    world.connect(fake_server(LEADER_FILES), "apps.protocols.operations")
    instance = instance_of(LEADER_HOST, LEADER_FILES)
    settings, key = _new_header_protection_key(AwgSettings.of(instance))
    return world.run(
        lambda: start_settings_change(instance, settings, world.admin, reissue=False), key
    )


def group_settings(world: World) -> Started:
    world.connect(fake_server(LEADER_FILES), "apps.groups.services.settings")
    leader = instance_of(LEADER_HOST, LEADER_FILES)
    group = create_group(ServerGroup(name="Europe"), leader, actor=world.admin)
    settings, key = _new_header_protection_key(AwgSettings.of(leader))
    return world.run(
        lambda: start_group_settings(group, settings, user=world.admin, reissue=False), key
    )


def service_settings(world: World) -> Started:
    world.connect(FakeProxyHost(), "apps.protocols.operations")
    installer = installers.get(SOCKS5_CONTAINER)
    request = InstallRequest(installer, installer.default_port)
    server = ServerFactory(host="203.0.113.7")
    world.run(lambda: start_install(server, request, world.admin), "")
    service = ServiceInstance.objects.get(container=SOCKS5_CONTAINER)
    password = "Pass_word1"
    settings = ServiceSettings(service.port or 0, {"user": "alice"}, password)
    return world.run(
        lambda: start_service_settings_change(service, settings, world.admin), password
    )


EXTRA_SECRET = "99887766554433221100ffeeddccbbaa"


def _install_mtproxy(world: World, secrets: tuple[str, ...]) -> Started:
    world.connect(FakeProxyHost(), "apps.protocols.operations")
    installer = installers.get(MTPROXY_CONTAINER)
    proxy = TelegramProxyOptions(additional_secrets=secrets)
    request = InstallRequest(installer, installer.default_port, proxy=proxy)
    server = ServerFactory(host="203.0.113.7")
    return world.run(lambda: start_install(server, request, world.admin), EXTRA_SECRET)


def mtproxy_install(world: World) -> Started:
    return _install_mtproxy(world, (EXTRA_SECRET,))


def mtproxy_settings(world: World) -> Started:
    _install_mtproxy(world, ())
    service = ServiceInstance.objects.get(container=MTPROXY_CONTAINER)
    settings = ServiceSettings(service.port or 0, {"additional_secrets": [EXTRA_SECRET]})
    return world.run(
        lambda: start_service_settings_change(service, settings, world.admin), EXTRA_SECRET
    )


def panel_archive(world: World) -> Started:
    world.settings.PANEL_ARCHIVE_DIR = world.tmp_path / "archives"
    world.settings.MEDIA_ROOT = world.tmp_path / "media"
    (world.tmp_path / "media").mkdir()
    dump = FakeDump()
    world.monkeypatch.setattr(PostgresDump, "from_settings", staticmethod(lambda: dump))
    return world.run(lambda: start_panel_archive(PASSWORD, world.admin, None), PASSWORD)


@dataclass(frozen=True)
class SecretCase:
    kind: str
    param: str
    run: Callable[[World], Started]


CASES = (
    SecretCase(SETTINGS_KIND, "header_protection_key_token", awg_settings),
    SecretCase(GROUP_SETTINGS_KIND, "header_protection_key_token", group_settings),
    SecretCase(SERVICE_SETTINGS_KIND, "password_token", service_settings),
    SecretCase(PANEL_ARCHIVE_KIND, "password_token", panel_archive),
    SecretCase(INSTALL_KIND, "additional_secrets_token", mtproxy_install),
    SecretCase(SERVICE_SETTINGS_KIND, "additional_secrets_token", mtproxy_settings),
)


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    tmp_path: Path,
    django_capture_on_commit_callbacks: OnCommit,
) -> World:
    admin = UserFactory(role=Role.ADMIN)
    return World(admin, monkeypatch, settings, tmp_path, django_capture_on_commit_callbacks)


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.kind}:{case.param}")
def test_the_secret_stays_out_of_the_audit_the_steps_and_the_page(
    case: SecretCase, world: World, client: Client
) -> None:
    started = case.run(world)

    operation = started.operation
    assert operation.kind == case.kind
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert case.param in started.tokens
    client.force_login(world.admin)
    assert secrets_written(operation, started.secret, client) == []
    # The encrypted secret is for the job alone.
    assert set(secrets_written(operation, started.tokens[case.param], client)) <= {"params"}


def test_every_secret_param_has_a_case() -> None:
    assert {case.param for case in CASES} == set(Operation.SECRET_PARAMS)
