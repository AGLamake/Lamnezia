from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.test import Client

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH, AwgServerConfig
from amnezia_core.awg_install import upgraded_awg_params
from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD, WgContainer
from amnezia_core.keys import KeyPair
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.awg_settings import AwgSettings
from apps.protocols.services.settings_change import start_settings_change
from apps.protocols.tests.awg_case import (
    APP_CONFIG,
    PSK,
    Start,
    fixture,
    issued_key,
    server_config,
)
from apps.protocols.tests.fake_awg import PREVIOUS, START_SCRIPT, FakeAwgServer
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db


def settings_of(instance: ProtocolInstance, **changes: Any) -> AwgSettings:
    params = {**instance.params, **changes.pop("params", {})}
    return AwgSettings.build(
        changes.get("port", instance.port),
        params,
        changes.get("dns", instance.dns),
        logs=changes.get("logs", instance.container_logs),
        subnet=changes.get("subnet", instance.subnet),
    )


def test_new_parameters_restart_awg_and_keep_the_clients(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(instance, UserFactory(email="anna@example.com"))
    settings = settings_of(instance, params={"Jc": "8", "S1": "70"})

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    config = server_config(connected)
    assert config.awg_params["Jc"] == "8"
    assert config.private_key == APP_CONFIG.private_key
    assert config.peers == APP_CONFIG.peers
    assert connected.awg_up
    assert "run" not in connected.docker_calls
    instance.refresh_from_db()
    assert instance.params == settings.params
    key.refresh_from_db()
    assert ServerProfile.from_json(key.profile).params["Jc"] == "8"
    notification = Notification.objects.get(event=Event.KEYS_CHANGED)
    assert notification.user == key.user


def test_new_port_moves_the_files_into_a_new_container(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    settings = settings_of(instance, port=40000)

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert connected.published_port == 40000
    assert connected.previous is None
    assert connected.awg_up
    assert server_config(connected).listen_port == 40000
    assert server_config(connected).peers == APP_CONFIG.peers
    assert connected.container_files[AWG_CLIENTS_TABLE_PATH] == fixture("clientsTable.json")
    assert connected.container_files[AWG_PSK_PATH] == f"{PSK}\n".encode()
    assert b"awg-quick up" in connected.container_files[START_SCRIPT]
    assert "amnezia-dns-net" in connected.container_networks
    instance.refresh_from_db()
    assert instance.port == 40000


def test_new_subnet_moves_the_server_and_every_client_into_it(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(instance, UserFactory(email="anna@example.com"))
    settings = settings_of(instance, subnet="10.9.0.0/24")

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    config = server_config(connected)
    assert str(config.address) == "10.9.0.0/24"
    assert [peer.allowed_ips for peer in config.peers] == [("10.9.0.1/32",), ("10.9.0.2/32",)]
    assert b"10.9.0.0" in connected.container_files[START_SCRIPT]
    assert connected.previous is None and connected.awg_up
    instance.refresh_from_db()
    assert instance.subnet == "10.9.0.0/24"
    key.refresh_from_db()
    assert key.client_ip == "10.9.0.5"
    assert ServerProfile.from_json(key.profile).subnet == "10.9.0.0/24"
    assert Notification.objects.filter(event=Event.KEYS_CHANGED, user=key.user).exists()


def test_operation_queued_before_the_subnet_field_keeps_the_subnet(
    connected: FakeAwgServer, instance: ProtocolInstance
) -> None:
    current = AwgSettings.of(instance)
    old = {name: value for name, value in current.to_params().items() if name != "subnet"}

    assert AwgSettings.from_params(old).within(current) == current


def test_failed_move_brings_the_old_container_back(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    connected.awg_starts = False
    settings = settings_of(instance, port=40000)

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.FAILED
    assert connected.container_state == "running"
    assert connected.published_port == 55424
    assert connected.container_files[AWG_CONFIG_PATH] == fixture("awg0.conf")
    assert connected.previous is None
    instance.refresh_from_db()
    assert instance.port == 55424


def test_container_left_by_an_interrupted_move_stops_the_change(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    connected.previous = ("exited", {}, 55424)

    operation = start(
        lambda: start_settings_change(
            instance, settings_of(instance, port=40000), admin, reissue=False
        )
    )

    assert operation.status == OperationStatus.FAILED
    assert PREVIOUS in operation.error_hint or "previous" in operation.error_hint
    assert connected.published_port == 55424


def test_parameters_awg_refuses_are_rolled_back(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    connected.awg_refuses = "Jc = 9"
    settings = settings_of(instance, params={"Jc": "9"})

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.FAILED
    assert connected.container_files[AWG_CONFIG_PATH] == fixture("awg0.conf")
    assert connected.awg_up
    instance.refresh_from_db()
    assert instance.params["Jc"] == "4"


def test_dns_alone_changes_only_the_profiles(
    instance: ProtocolInstance, admin: User, start: Start, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apps.protocols.operations.connect", None)
    key = issued_key(instance)
    settings = settings_of(instance, dns=("9.9.9.9", "149.112.112.112"))

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    key.refresh_from_db()
    assert ServerProfile.from_json(key.profile).dns == ("9.9.9.9", "149.112.112.112")
    instance.refresh_from_db()
    assert instance.dns == ("9.9.9.9", "149.112.112.112")


def test_container_log_moves_the_files_into_a_new_container_on_the_same_port(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    connected.port_users = "docker-pr 1234 root udp *:55424"  # the old container holds it
    key = issued_key(instance, UserFactory(email="anna@example.com"))
    settings = settings_of(instance, logs=True)

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert connected.log_driver == "json-file"
    assert connected.published_port == 55424
    assert connected.previous is None
    assert server_config(connected).peers == APP_CONFIG.peers
    assert connected.awg_up
    instance.refresh_from_db()
    assert instance.container_logs
    key.refresh_from_db()
    assert ServerProfile.from_json(key.profile).port == 55424
    assert not Notification.objects.filter(event=Event.KEYS_CHANGED).exists()


def test_container_log_already_kept_leaves_the_container(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    connected.log_driver = "json-file"
    settings = settings_of(instance, logs=True)

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert "run" not in connected.docker_calls
    assert "rename" not in connected.docker_calls


def test_imported_keys_are_reissued_on_request(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    imported = AccessKey.objects.create(
        source=instance.source,
        name="laptop",
        public_key="imported",
        status=KeyStatus.ACTIVE,
        imported=True,
    )
    issued_key(instance)
    settings = settings_of(instance, params={"Jc": "8"})

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=True))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    batch = Operation.objects.get(kind="keys.reissue")
    new = AccessKey.objects.get(source=instance.source, name="laptop", imported=False)
    assert batch.params == {"source": instance.source, "pairs": [[imported.pk, new.pk, "active"]]}
    assert not Operation.objects.filter(kind__in=["keys.issue", "keys.revoke"]).exists()


@pytest.fixture
def admin_client(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def form_data(instance: ProtocolInstance, **changes: str) -> dict[str, str]:
    return {
        "port": str(instance.port),
        "subnet": instance.subnet,
        "primary_dns": instance.primary_dns,
        "secondary_dns": instance.secondary_dns,
        **instance.params,
        **changes,
    }


def test_settings_page_shows_the_current_values(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    html = admin_client.get(f"/protocols/{instance.pk}/settings/").content.decode()

    assert 'value="55424"' in html
    assert 'value="1020325451-1025713284"' in html
    # The port is above 9999: the warning is shown at once, with the wiki and the speed test.
    assert "{ port: '55424' }" in html
    assert "port > 9999" in html
    assert f"/protocols/speed-test/{instance.server.pk}/" in html


@pytest.mark.parametrize(
    "changes",
    [
        {"port": "70000"},
        {"primary_dns": "dns.google"},
        {"Jc": "many"},
        {"H2": "1020325451"},
        {"S1": "0", "S2": "56"},
        {"I1": "<x 5>"},
        {"port": "3478", "confirm": "on"},
    ],
)
def test_invalid_settings_cannot_be_saved(
    admin_client: Client, instance: ProtocolInstance, changes: dict[str, str]
) -> None:
    response = admin_client.post(
        f"/protocols/{instance.pk}/settings/", form_data(instance, **{"confirm": "on", **changes})
    )

    assert response.status_code == 200
    assert response.context["form"].errors
    assert not Operation.objects.exists()


def test_header_protection_with_small_padding_is_refused(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    key = KeyPair.generate().private_key
    response = admin_client.post(
        f"/protocols/{instance.pk}/settings/",
        form_data(instance, confirm="on", HeaderProtectionKey=key, S4="8"),
    )

    assert set(response.context["form"].errors) == {"S4"}
    assert not Operation.objects.exists()


def test_settings_of_awg2_show_its_version_and_ask_to_move_first(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    response = admin_client.get(f"/protocols/{instance.pk}/settings/")

    html = response.content.decode()
    assert "version 2" in html
    assert "calls AmneziaWG 2.0 outdated" in html
    group = next(g for g in response.context["form"].groups if g.title == "AmneziaWG 3.1")
    assert group.fields[0].name == "HeaderProtectionKey"
    assert group.note


def test_settings_of_awg3_have_no_reminder(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    instance.params = upgraded_awg_params(instance.params)
    instance.save()

    response = admin_client.get(f"/protocols/{instance.pk}/settings/")

    html = response.content.decode()
    assert "version 3.1" in html
    assert "outdated" not in html
    assert not any(group.note for group in response.context["form"].groups)


def test_change_of_the_parameters_must_be_confirmed(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    url = f"/protocols/{instance.pk}/settings/"

    refused = admin_client.post(url, form_data(instance, Jc="8"))
    accepted = admin_client.post(url, form_data(instance, Jc="8", confirm="on"))

    assert "confirm" in refused.context["form"].errors
    operation = Operation.objects.get()
    assert accepted.headers["Location"] == f"/operations/{operation.pk}/"
    assert operation.params["params"]["Jc"] == "8"
    assert operation.params["reissue"] is False


def test_new_subnet_must_be_confirmed(admin_client: Client, instance: ProtocolInstance) -> None:
    url = f"/protocols/{instance.pk}/settings/"

    refused = admin_client.post(url, form_data(instance, subnet="10.9.0.0/23"))
    admin_client.post(url, form_data(instance, subnet="10.9.0.0/23", confirm="on"))

    assert set(refused.context["form"].errors) == {"confirm"}
    assert Operation.objects.get().params["subnet"] == "10.9.0.0/23"


@pytest.mark.parametrize(
    ("subnet", "error"),
    [
        ("10.9.0.1/24", "like 10.8.1.0/24"),
        ("10.9.0.0/25", "at least /24"),
        ("8.8.8.0/24", "private network"),
        ("172.29.172.0/24", "overlaps"),
        ("10.8.0.0/16", "overlaps"),
    ],
)
def test_subnet_that_does_not_fit_is_refused(
    admin_client: Client, instance: ProtocolInstance, subnet: str, error: str
) -> None:
    ProtocolInstance.objects.create(
        server=instance.server, container="amnezia-openvpn", port=1194, subnet="10.8.0.0/24"
    )

    response = admin_client.post(
        f"/protocols/{instance.pk}/settings/", form_data(instance, subnet=subnet, confirm="on")
    )

    assert error in str(response.context["form"].errors["subnet"])
    assert not Operation.objects.exists()


def test_dns_change_needs_no_confirmation(admin_client: Client, instance: ProtocolInstance) -> None:
    admin_client.post(
        f"/protocols/{instance.pk}/settings/", form_data(instance, primary_dns="9.9.9.9")
    )

    assert Operation.objects.get().params["dns"] == ["9.9.9.9", "1.0.0.1"]


def test_container_log_needs_no_confirmation(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    admin_client.post(
        f"/protocols/{instance.pk}/settings/", form_data(instance, container_logs="on")
    )

    assert Operation.objects.get().params["logs"] is True


def test_settings_are_for_admins_only(client: Client, instance: ProtocolInstance) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get(f"/protocols/{instance.pk}/settings/").status_code == 403


LEGACY_CONFIG = """\
[Interface]
PrivateKey = {key}
Address = 10.8.1.0/24
ListenPort = 51820
Jc = 4
Jmin = 10
Jmax = 50
S1 = 50
S2 = 100
H1 = 11
H2 = 22
H3 = 33
H4 = 44
"""
WIREGUARD_CONFIG = """\
[Interface]
PrivateKey = {key}
Address = 10.8.1.0/24
ListenPort = 51820
"""


@pytest.mark.parametrize(("kind", "params"), [(AWG_LEGACY, {"Jc": "6"}), (WIREGUARD, {})])
def test_other_wireguard_family_containers_move_to_a_new_port(
    monkeypatch: pytest.MonkeyPatch,
    admin: User,
    start: Start,
    kind: WgContainer,
    params: dict[str, str],
) -> None:
    pair = KeyPair.generate()
    text = LEGACY_CONFIG if kind == AWG_LEGACY else WIREGUARD_CONFIG
    config = text.format(key=pair.private_key)
    server = FakeAwgServer(
        kind=kind,
        container_state="running",
        container_networks={"bridge", "amnezia-dns-net"},
        container_files={
            kind.config_path: config.encode(),
            kind.psk_path: f"{PSK}\n".encode(),
            kind.private_key_path: f"{pair.private_key}\n".encode(),
            START_SCRIPT: f"#!/bin/bash\n{kind.tool}-quick up {kind.config_path}\n".encode(),
        },
        awg_up=True,
        published_port=51820,
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    current = AwgServerConfig.parse(config)
    instance = ProtocolInstance.objects.create(
        server=ServerFactory(host="203.0.113.10"),
        container=kind.name,
        port=51820,
        subnet="10.8.1.0/24",
        params=current.awg_params,
        public_key=pair.public_key,
        private_key=pair.private_key,
        preshared_key=PSK,
        origin=InstanceOrigin.IMPORTED,
    )
    settings = settings_of(instance, port=40000, params=params)

    operation = start(lambda: start_settings_change(instance, settings, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    moved = AwgServerConfig.parse(server.container_files[kind.config_path].decode())
    assert moved.listen_port == 40000
    assert moved.awg_params == {**current.awg_params, **params}
    assert server.container_files[kind.private_key_path] == f"{pair.private_key}\n".encode()
    assert server.published_port == 40000
    assert server.previous is None
    assert server.awg_up


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [(AWG_LEGACY, "Jc", "S3"), (WIREGUARD, "port", "Jc"), (AWG2, "S4", "")],
)
def test_settings_page_shows_the_parameters_of_the_container(
    admin_client: Client, kind: WgContainer, present: str, absent: str
) -> None:
    instance = ProtocolInstance.objects.create(
        server=ServerFactory(),
        container=kind.name,
        port=51820,
        subnet="10.8.1.0/24",
        params={},
        origin=InstanceOrigin.INSTALLED,
    )

    form = admin_client.get(f"/protocols/{instance.pk}/settings/").context["form"]

    assert present in form.fields
    assert not absent or absent not in form.fields
