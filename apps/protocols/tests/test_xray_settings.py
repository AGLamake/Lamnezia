"""XRay settings change from the panel: server.json, the container and the key profiles."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from amnezia_core.xray import XRAY_CONFIG_PATH, XrayServerConfig, client_fingerprint
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey
from apps.keys.services.profile import XrayKeyProfile
from apps.keys.signals import xray_restarting
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.settings_change import start_settings_change
from apps.protocols.services.xray_settings import XrayPanelSettings
from apps.protocols.tests.fake_xray_host import (
    GENERATED,
    OWN_CLIENT,
    PUBLIC_KEY,
    SHORT_ID,
    FakeXrayHost,
)
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
SERVER_JSON = (FIXTURES / "xray_server.json").read_text(encoding="utf-8")
START = b"#!/bin/bash\nxray -config /opt/amnezia/xray/server.json\n"
CLIENT = "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e"
OnCommit = Callable[..., AbstractContextManager[Any]]


@pytest.fixture
def xray(monkeypatch: pytest.MonkeyPatch) -> FakeXrayHost:
    config = XrayServerConfig.parse(SERVER_JSON)
    config.add_client(CLIENT, "xtls-rprx-vision")
    server = FakeXrayHost(
        container_state="running",
        container_networks={"bridge", "amnezia-dns-net"},
        start_script=START,
        volume={
            XRAY_CONFIG_PATH: config.serialize().encode(),
            **{path: f"{value}\n".encode() for path, value in GENERATED.items()},
        },
        xray_up=True,
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return server


@pytest.fixture
def restarts() -> Iterator[list[str]]:
    """Sources the traffic collector would read the counters of before a restart."""
    sources: list[str] = []

    def receiver(sender: object, source: str, **kwargs: Any) -> None:
        sources.append(source)

    xray_restarting.connect(receiver)
    try:
        yield sources
    finally:
        xray_restarting.disconnect(receiver)


@pytest.fixture
def instance() -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=ServerFactory(host="203.0.113.10"),
        container="amnezia-xray",
        port=443,
        subnet="",
        params={
            "site": "www.googletagmanager.com",
            "sni": "www.googletagmanager.com",
            "flow": "xtls-rprx-vision",
            "fingerprint": "chrome",
        },
        public_key=PUBLIC_KEY,
        origin=InstanceOrigin.INSTALLED,
    )


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def key(instance: ProtocolInstance, admin: User) -> AccessKey:
    profile = XrayKeyProfile(
        "203.0.113.10",
        443,
        "xtls-rprx-vision",
        "www.googletagmanager.com",
        "www.googletagmanager.com",
        "chrome",
        PUBLIC_KEY,
        SHORT_ID,
    )
    return AccessKey.objects.create(
        user=admin,
        source=instance.source,
        name="Phone",
        public_key=client_fingerprint(CLIENT),
        private_key=CLIENT,
        profile=profile.to_json(),
    )


def change(
    instance: ProtocolInstance,
    admin: User | None,
    capture: OnCommit,
    **fields: Any,
) -> Operation:
    assert admin is not None
    current = XrayPanelSettings.of(instance).to_params()
    values = {**current, **fields}
    settings = XrayPanelSettings.build(**values)
    with capture(execute=True):
        operation = start_settings_change(instance, settings, admin, reissue=False)
    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    return operation


def server_config(xray: FakeXrayHost) -> XrayServerConfig:
    return XrayServerConfig.parse(xray.volume[XRAY_CONFIG_PATH].decode())


def test_site_and_flow_rewrite_server_json_and_keep_clients_and_stats(
    xray: FakeXrayHost,
    restarts: list[str],
    instance: ProtocolInstance,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    change(
        instance,
        key.user,
        django_capture_on_commit_callbacks,
        site="www.microsoft.com",
        sni="",
        flow="",
    )

    config = server_config(xray)
    assert config.settings().site == "www.microsoft.com"
    assert config.settings().flow == ""
    assert config.client_ids == [OWN_CLIENT, CLIENT]
    assert config.stats_enabled
    assert xray.restarts == 1 and xray.previous is None
    assert restarts == [instance.source]
    profile = XrayKeyProfile.from_json(AccessKey.objects.get(pk=key.pk).profile)
    assert (profile.site, profile.sni, profile.flow) == ("www.microsoft.com",) * 2 + ("",)
    instance.refresh_from_db()
    assert instance.params["site"] == "www.microsoft.com"


def test_new_port_moves_the_volume_into_a_new_container(
    xray: FakeXrayHost,
    restarts: list[str],
    instance: ProtocolInstance,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    change(instance, key.user, django_capture_on_commit_callbacks, port=8443)

    assert "8443:8443/tcp" in xray.run_arguments and "8443:8443/udp" in xray.run_arguments
    assert b"--dport 8443" in xray.start_script
    config = server_config(xray)
    assert config.settings().port == 8443
    assert config.client_ids == [OWN_CLIENT, CLIENT]
    assert config.stats_enabled
    assert xray.xray_up and xray.previous is None
    assert restarts == [instance.source]
    assert XrayKeyProfile.from_json(AccessKey.objects.get(pk=key.pk).profile).port == 8443
    instance.refresh_from_db()
    assert instance.port == 8443


def test_old_container_comes_back_with_its_server_json_when_xray_fails(
    xray: FakeXrayHost,
    instance: ProtocolInstance,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    before = xray.volume[XRAY_CONFIG_PATH]
    xray.xray_starts = False
    settings = XrayPanelSettings.build(
        port=8443, flow="xtls-rprx-vision", site="www.googletagmanager.com", sni="",
        fingerprint="chrome",
    )  # fmt: skip

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_settings_change(instance, settings, admin, reissue=False)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert xray.volume[XRAY_CONFIG_PATH] == before
    assert xray.container_state == "running" and xray.previous is None
    assert "443:443/tcp" in xray.run_arguments or not xray.run_arguments
    instance.refresh_from_db()
    assert instance.port == 443


def test_fingerprint_only_changes_the_profiles(
    monkeypatch: pytest.MonkeyPatch,
    instance: ProtocolInstance,
    admin: User,
    key: AccessKey,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    monkeypatch.setattr("apps.protocols.operations.connect", None)

    change(instance, admin, django_capture_on_commit_callbacks, fingerprint="qq")

    assert XrayKeyProfile.from_json(AccessKey.objects.get(pk=key.pk).profile).fingerprint == "qq"


def test_settings_page_of_xray_offers_the_fields_of_the_app(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    response = admin_client.get(f"/protocols/{instance.pk}/settings/")

    form = response.context["form"]
    assert {"port", "site", "sni", "flow", "fingerprint"} <= set(form.fields)
    assert form.initial["site"] == "www.googletagmanager.com"
