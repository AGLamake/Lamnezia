import json

import pytest
from django.test import Client
from django.urls import reverse

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH
from amnezia_core.awg_export import has_awg3_params
from amnezia_core.awg_install import upgraded_awg_params
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus, OperationStep
from apps.protocols.errors import SetupErrorCode
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_upgrade import UPGRADE_KIND, can_upgrade, start_awg_upgrade
from apps.protocols.tests.awg_case import (
    APP_CONFIG,
    PSK,
    Start,
    fixture,
    issued_key,
    server_config,
)
from apps.protocols.tests.fake_awg import FakeAwgServer

pytestmark = pytest.mark.django_db


def secrets_written(operation: Operation, key: str) -> list[str]:
    """Where the operation, its steps or the audit carry `key`."""
    places = {
        "params": json.dumps(operation.params),
        "error": operation.error_message + operation.error_hint,
        "steps": "".join(
            OperationStep.objects.filter(operation=operation).values_list("log", flat=True)
        ),
        "audit": json.dumps(list(AuditEvent.objects.values_list("data", flat=True))),
    }
    return [place for place, text in places.items() if key in text]


def test_server_moves_to_awg3_with_its_keys_and_clients(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(instance, UserFactory(email="anna@example.com"))

    operation = start(lambda: start_awg_upgrade(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert operation.kind == UPGRADE_KIND
    assert "build" in connected.docker_calls
    config = server_config(connected)
    assert has_awg3_params(config.awg_params)
    assert {name: config.awg_params[name] for name in APP_CONFIG.awg_params} == (
        APP_CONFIG.awg_params
    )
    assert config.private_key == APP_CONFIG.private_key
    assert config.peers == APP_CONFIG.peers
    assert config.listen_port == 55424
    assert connected.container_files[AWG_CLIENTS_TABLE_PATH] == fixture("clientsTable.json")
    assert connected.container_files[AWG_PSK_PATH] == f"{PSK}\n".encode()
    assert connected.published_port == 55424
    assert connected.previous is None
    assert connected.awg_up
    instance.refresh_from_db()
    assert instance.params == config.awg_params
    assert not can_upgrade(instance)
    key.refresh_from_db()
    assert ServerProfile.from_json(key.profile).params == config.awg_params
    assert Notification.objects.get(event=Event.KEYS_CHANGED).user == key.user
    assert secrets_written(operation, config.awg_params["HeaderProtectionKey"]) == []


FAULTS = {
    SetupErrorCode.AWG_NOT_RUNNING: ("awg_starts", False),
    SetupErrorCode.AWG3_DROPPED: ("drops_awg3", True),
    SetupErrorCode.IMAGE_BUILD_FAILED: ("build_error", b"no space left on device"),
}


@pytest.mark.parametrize("code", list(FAULTS))
def test_failed_upgrade_leaves_the_server_on_awg2(
    connected: FakeAwgServer,
    instance: ProtocolInstance,
    admin: User,
    start: Start,
    code: SetupErrorCode,
) -> None:
    key = issued_key(instance)
    profile = key.profile
    setattr(connected, *FAULTS[code])

    operation = start(lambda: start_awg_upgrade(instance, admin, reissue=False))

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == code
    assert connected.container_state == "running"
    assert connected.published_port == 55424
    assert connected.container_files[AWG_CONFIG_PATH] == fixture("awg0.conf")
    assert connected.previous is None
    instance.refresh_from_db()
    assert instance.params == APP_CONFIG.awg_params
    assert can_upgrade(instance)
    key.refresh_from_db()
    assert key.profile == profile


def test_server_already_on_awg3_is_only_read(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    """A retry after the panel failed to save: the server is not moved again."""
    params = upgraded_awg_params(APP_CONFIG.awg_params)
    config = server_config(connected)
    config.update_interface(55424, params)
    connected.container_files[AWG_CONFIG_PATH] = config.serialize().encode()

    operation = start(lambda: start_awg_upgrade(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert "build" not in connected.docker_calls
    assert "run" not in connected.docker_calls
    instance.refresh_from_db()
    assert instance.params == params


def test_imported_keys_are_reissued_on_request(
    connected: FakeAwgServer, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    AccessKey.objects.create(
        source=instance.source,
        name="laptop",
        public_key="imported",
        status=KeyStatus.ACTIVE,
        imported=True,
    )

    operation = start(lambda: start_awg_upgrade(instance, admin, reissue=True))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert operation.params == {"instance_id": instance.pk, "user_id": admin.pk, "reissue": True}
    assert Operation.objects.filter(kind="keys.reissue").exists()


def test_only_awg2_servers_can_be_upgraded(instance: ProtocolInstance) -> None:
    assert can_upgrade(instance)

    instance.container = "amnezia-awg"
    assert not can_upgrade(instance)


def test_card_offers_the_upgrade(admin_client: Client, instance: ProtocolInstance) -> None:
    page = admin_client.get(reverse("servers:detail", args=[instance.server_id]))

    assert reverse("protocols:awg-upgrade", args=[instance.pk]).encode() in page.content
    assert b"version 2<" in page.content
    assert b"calls AmneziaWG 2.0 outdated" in page.content


def test_card_offers_no_upgrade_to_awg3_server(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    instance.params = upgraded_awg_params(instance.params)
    instance.save()

    page = admin_client.get(reverse("servers:detail", args=[instance.server_id]))

    assert reverse("protocols:awg-upgrade", args=[instance.pk]).encode() not in page.content
    assert b"version 3.1<" in page.content
    assert b"outdated" not in page.content
    response = admin_client.post(reverse("protocols:awg-upgrade", args=[instance.pk]))
    assert response.status_code == 404


def test_upgrade_button_starts_the_operation(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    response = admin_client.post(
        reverse("protocols:awg-upgrade", args=[instance.pk]), {"reissue": "on"}
    )

    operation = Operation.objects.get(kind=UPGRADE_KIND)
    assert response.url == reverse("operations:detail", args=[operation.pk])  # type: ignore[attr-defined]
    assert operation.params["reissue"] is True
    assert AuditEvent.objects.get(action=UPGRADE_KIND).data == {"operation": operation.pk}


def test_upgrade_is_for_admins_only(client: Client, instance: ProtocolInstance) -> None:
    client.force_login(UserFactory())

    response = client.post(reverse("protocols:awg-upgrade", args=[instance.pk]))

    assert response.status_code in (403, 404)
    assert not Operation.objects.filter(kind=UPGRADE_KIND).exists()
