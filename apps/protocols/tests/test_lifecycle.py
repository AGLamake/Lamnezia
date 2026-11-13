from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from amnezia_core.containers import WIREGUARD
from amnezia_core.ipsec import IKEV2_CONFIG_PATH, IPSEC_CONTAINER
from amnezia_core.openvpn import OPENVPN_CONFIG_PATH, OPENVPN_CONTAINER
from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_CONTAINER
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.backups.models import ProtocolBackup
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.signals import xray_restarting
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.lifecycle import (
    LOGS_KIND,
    REMOVE_KIND,
    RESTART_KIND,
    STOP_KIND,
    start_container_action,
)
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer
from apps.protocols.tests.fake_ipsec_host import FakeIpsecHost
from apps.protocols.tests.fake_openvpn_host import PKI_FILES, FakeOpenVpnHost
from apps.protocols.tests.fake_xray_host import FakeXrayHost
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
START = b"#!/bin/bash\nawg-quick up /opt/amnezia/awg/awg0.conf\n"


@pytest.fixture
def awg(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    server = FakeAwgServer(
        container_state="running", container_files={START_SCRIPT: START}, awg_up=True
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return server


@pytest.fixture
def instance() -> ProtocolInstance:
    server = ServerFactory(
        name="Frankfurt", containers=[{"name": "amnezia-awg2", "port": 55424, "transport": "udp"}]
    )
    return ProtocolInstance.objects.create(
        server=server,
        container="amnezia-awg2",
        port=55424,
        subnet="10.8.1.0/24",
        public_key="server-public",
        private_key="server-private",
        origin=InstanceOrigin.IMPORTED,
    )


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def run(
    django_capture_on_commit_callbacks: OnCommit, instance: ProtocolInstance, admin: User
) -> Callable[..., Operation]:
    """Start a container action and run it at once, as the worker would."""

    def runner(kind: str, **options: bool) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = start_container_action(kind, instance, admin, **options)
        operation.refresh_from_db()
        return operation

    return runner


def step_log(operation: Operation) -> str:
    return "".join(step.log for step in operation.steps.all())


def test_restart_starts_a_stopped_container_and_waits_for_awg(
    awg: FakeAwgServer, run: Callable[..., Operation]
) -> None:
    awg.container_state, awg.awg_up = "exited", False

    operation = run(RESTART_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.container_state == "running"
    assert awg.awg_up
    assert awg.locks == 1
    assert AuditEvent.objects.filter(action=RESTART_KIND).exists()


def test_restart_fails_when_awg_does_not_come_up(
    awg: FakeAwgServer, run: Callable[..., Operation]
) -> None:
    awg.awg_starts = False

    assert run(RESTART_KIND).status == OperationStatus.FAILED


def test_stop_leaves_the_container_on_the_server(
    awg: FakeAwgServer, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    operation = run(STOP_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.container_state == "exited"
    assert ProtocolInstance.objects.filter(pk=instance.pk).exists()


def backup_of(instance: ProtocolInstance) -> ProtocolBackup:
    return ProtocolBackup.objects.create(
        instance=instance, config="[Interface]\n", clients_table="[]", taken_at=timezone.now()
    )


def test_removal_keeps_the_keys_and_the_backup_for_a_restore(
    awg: FakeAwgServer, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    key = AccessKey.objects.create(
        source=instance.source, name="iPhone", public_key="peer", status=KeyStatus.ACTIVE
    )
    backup_of(instance)

    operation = run(REMOVE_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.container_state == ""
    assert not awg.has_image
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    instance.refresh_from_db()
    assert instance.removed
    assert ProtocolBackup.objects.filter(instance=instance).exists()
    assert Server.objects.get().containers == []


def test_removal_with_the_backups_revokes_the_keys_and_tells_their_owners(
    awg: FakeAwgServer, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    owner = UserFactory(email="anna@example.com")
    key = AccessKey.objects.create(
        user=owner,
        source=instance.source,
        name="iPhone",
        public_key="peer",
        status=KeyStatus.ACTIVE,
    )
    backup_of(instance)

    operation = run(REMOVE_KIND, delete_backups=True)

    assert operation.status == OperationStatus.SUCCEEDED
    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKED
    assert key.revoked_at is not None
    assert not ProtocolInstance.objects.exists()
    assert not ProtocolBackup.objects.exists()
    notification = Notification.objects.get(event=Event.KEYS_REVOKED)
    assert notification.user == owner
    assert notification.data["server"] == "Frankfurt"


def test_removal_of_a_container_already_gone_still_forgets_the_instance(
    awg: FakeAwgServer, run: Callable[..., Operation]
) -> None:
    awg.container_state = ""

    operation = run(REMOVE_KIND, delete_backups=True)

    assert operation.status == OperationStatus.SUCCEEDED
    assert "rm" not in awg.docker_calls
    assert not ProtocolInstance.objects.exists()


def test_backups_of_a_removed_container_are_deleted_without_the_server(
    awg: FakeAwgServer, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    run(REMOVE_KIND)
    awg.docker_calls.clear()
    instance.refresh_from_db()

    operation = run(REMOVE_KIND, delete_backups=True)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.docker_calls == []
    assert not ProtocolInstance.objects.exists()


def test_removed_container_has_no_settings_and_actions(
    admin_client: Client,
    awg: FakeAwgServer,
    run: Callable[..., Operation],
    instance: ProtocolInstance,
) -> None:
    run(REMOVE_KIND)

    card = admin_client.get(f"/servers/{instance.server.pk}/").content.decode()

    assert "container removed" in card
    assert f"/protocols/{instance.pk}/settings/" not in card
    assert admin_client.get(f"/protocols/{instance.pk}/settings/").status_code == 404
    assert admin_client.post(f"/protocols/{instance.pk}/container/restart/").status_code == 404
    assert admin_client.get(f"/protocols/{instance.pk}/container/remove/").status_code == 200


def test_log_explains_that_amnezia_containers_keep_none(
    awg: FakeAwgServer, run: Callable[..., Operation]
) -> None:
    operation = run(LOGS_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert "running · restarts 0" in step_log(operation)
    assert "logging turned off" in step_log(operation)
    assert "protocol settings" in step_log(operation)
    assert awg.locks == 0
    assert not AuditEvent.objects.exists()


def test_log_shows_the_last_lines(awg: FakeAwgServer, run: Callable[..., Operation]) -> None:
    awg.container_log = b"Container startup\n[#] ip link add awg0 type amneziawg\n"

    assert "ip link add awg0" in step_log(run(LOGS_KIND))


@pytest.mark.parametrize("action", ["restart", "stop", "logs"])
def test_card_buttons_start_the_operation(
    admin_client: Client, instance: ProtocolInstance, action: str
) -> None:
    response = admin_client.post(f"/protocols/{instance.pk}/container/{action}/")

    operation = Operation.objects.get()
    assert response.headers["Location"] == f"/operations/{operation.pk}/"


def test_unknown_action_is_not_found(admin_client: Client, instance: ProtocolInstance) -> None:
    response = admin_client.post(f"/protocols/{instance.pk}/container/reboot/")

    assert response.status_code == 404


def test_removal_needs_the_server_name(admin_client: Client, instance: ProtocolInstance) -> None:
    url = f"/protocols/{instance.pk}/container/remove/"

    page = admin_client.get(url)
    refused = admin_client.post(url, {"server_name": "frankfurt"})
    accepted = admin_client.post(url, {"server_name": " Frankfurt ", "delete_backups": "on"})

    assert "Frankfurt" in page.content.decode()
    assert "server_name" in refused.context["form"].errors
    operation = Operation.objects.get()
    assert operation.kind == REMOVE_KIND
    assert operation.params["delete_backups"] is True
    assert accepted.headers["Location"] == f"/operations/{operation.pk}/"


def test_container_actions_are_for_admins_only(client: Client, instance: ProtocolInstance) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.post(f"/protocols/{instance.pk}/container/restart/").status_code == 403
    assert client.get(f"/protocols/{instance.pk}/container/remove/").status_code == 403
    assert not Operation.objects.exists()


def test_wireguard_is_restarted_with_wg_and_has_a_settings_page(
    admin_client: Client,
    awg: FakeAwgServer,
    run: Callable[..., Operation],
    instance: ProtocolInstance,
) -> None:
    ProtocolInstance.objects.filter(pk=instance.pk).update(container=WIREGUARD.name)
    instance.server.containers = [{"name": WIREGUARD.name, "port": 51820, "transport": "udp"}]
    instance.server.save()
    awg.kind = WIREGUARD
    awg.container_files = {
        START_SCRIPT: b"#!/bin/bash\nwg-quick up /opt/amnezia/wireguard/wg0.conf\n"
    }
    awg.container_state, awg.awg_up = "exited", False

    operation = run(RESTART_KIND)
    card = admin_client.get(f"/servers/{instance.server.pk}/").content.decode()

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.awg_up
    assert "WireGuard" in card
    assert f"/protocols/{instance.pk}/settings/" in card
    assert admin_client.get(f"/protocols/{instance.pk}/settings/").status_code == 200
    assert admin_client.get(f"/protocols/{instance.pk}/container/remove/").status_code == 200


def test_xray_is_restarted_and_removed_and_has_a_settings_page(
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[..., Operation],
    instance: ProtocolInstance,
) -> None:
    ProtocolInstance.objects.filter(pk=instance.pk).update(container=XRAY_CONTAINER, port=443)
    xray = FakeXrayHost(
        container_state="exited",
        start_script=b"#!/bin/bash\nxray -config /opt/amnezia/xray/server.json\n",
        volume={XRAY_CONFIG_PATH: b"{}"},
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield xray.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    sent: list[str] = []

    def receiver(sender: object, source: str, **kwargs: Any) -> None:
        sent.append(source)

    xray_restarting.connect(receiver)
    try:
        operation = run(RESTART_KIND)
    finally:
        xray_restarting.disconnect(receiver)

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert sent == [ProtocolInstance.objects.get(pk=instance.pk).source]
    assert (xray.container_state, xray.xray_up) == ("running", True)
    assert "Wait for XRay to start" in [step.title for step in operation.steps.all()]
    assert admin_client.get(f"/protocols/{instance.pk}/settings/").status_code == 200
    assert admin_client.get(f"/protocols/{instance.pk}/container/remove/").status_code == 200


def test_openvpn_restart_waits_for_openvpn(
    monkeypatch: pytest.MonkeyPatch, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    ProtocolInstance.objects.filter(pk=instance.pk).update(container=OPENVPN_CONTAINER, port=1194)
    openvpn = FakeOpenVpnHost(
        container_state="exited",
        files={
            **PKI_FILES,
            OPENVPN_CONFIG_PATH: b"port 1194\n",
            "/opt/amnezia/start.sh": b"openvpn --config /opt/amnezia/openvpn/server.conf\n",
        },
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield openvpn.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)

    operation = run(RESTART_KIND)

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert openvpn.openvpn_up
    assert "Wait for OpenVPN to start" in [step.title for step in operation.steps.all()]


def test_ipsec_restart_starts_pluto_again(
    monkeypatch: pytest.MonkeyPatch, run: Callable[..., Operation], instance: ProtocolInstance
) -> None:
    """start.sh of amnezia-ipsec is empty: pluto comes back only by the service script."""
    ProtocolInstance.objects.filter(pk=instance.pk).update(container=IPSEC_CONTAINER, port=500)
    conf = b"conn ikev2-cp\n  leftid=203.0.113.7\n"
    ipsec = FakeIpsecHost(
        container_state="running", has_ca=True, pluto_up=True, files={IKEV2_CONFIG_PATH: conf}
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield ipsec.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)

    operation = run(RESTART_KIND)

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert ipsec.pluto_up
    assert (ipsec.service_runs, ipsec.pki_runs) == (1, 0)
    assert "Wait for IPsec to start" in [step.title for step in operation.steps.all()]
