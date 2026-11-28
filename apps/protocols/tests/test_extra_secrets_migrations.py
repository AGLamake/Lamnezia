"""The data migrations that seal the additional proxy secrets of older records."""

import importlib
import json

import pytest
from django.apps import apps

from apps.audit.models import AuditEvent
from apps.operations.models import Operation
from apps.protocols.models import InstanceOrigin, ServiceInstance
from apps.protocols.services.extra_secrets import open_extra_secrets
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

EXTRA = "00112233445566778899aabbccddeeff"
OTHER = "ffeeddccbbaa99887766554433221100"

services_migration = importlib.import_module("apps.protocols.migrations.0014_service_extra_secrets")
records_migration = importlib.import_module("apps.protocols.migrations.0015_seal_old_extra_secrets")


def test_the_secrets_of_a_service_move_out_of_its_settings() -> None:
    service = ServiceInstance.objects.create(
        server=ServerFactory(),
        container="amnezia-mtproxy",
        settings={"mode": "faketls", "additional_secrets": f"{EXTRA},{OTHER}"},
        origin=InstanceOrigin.IMPORTED,
    )

    services_migration.seal(apps, None)

    service.refresh_from_db()
    assert service.settings == {"mode": "faketls"}
    assert service.extra_secrets == f"{EXTRA},{OTHER}"

    services_migration.unseal(apps, None)

    service.refresh_from_db()
    assert service.settings["additional_secrets"] == f"{EXTRA},{OTHER}"


def test_old_operations_keep_the_secrets_encrypted_and_the_journal_drops_them() -> None:
    install = Operation.objects.create(
        kind="protocols.install", params={"port": 443, "proxy": {"additional_secrets": [EXTRA]}}
    )
    change = Operation.objects.create(
        kind="protocols.change_service_settings",
        params={"port": 443, "options": {"tag": "", "additional_secrets": [EXTRA, OTHER]}},
    )
    untouched = Operation.objects.create(kind="protocols.install", params={"proxy": {"tag": ""}})
    event = AuditEvent.objects.create(
        action="protocols.install",
        data={"proxy": {"mode": "faketls", "additional_secrets": [EXTRA]}},
    )

    records_migration.seal(apps, None)

    for operation, section, secrets in (
        (install, "proxy", [EXTRA]),
        (change, "options", [EXTRA, OTHER]),
    ):
        operation.refresh_from_db()
        assert EXTRA not in json.dumps(operation.params)
        options = open_extra_secrets(operation.params[section], operation.params)
        assert options["additional_secrets"] == secrets
    untouched.refresh_from_db()
    assert untouched.params == {"proxy": {"tag": ""}}
    event.refresh_from_db()
    assert event.data == {"proxy": {"mode": "faketls"}}
