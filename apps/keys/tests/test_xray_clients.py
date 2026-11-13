import json
from typing import Any

import pytest

from amnezia_core.clients_table import ClientsTable
from amnezia_core.xray import (
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_OWN_CLIENT_PATH,
    XrayServerConfig,
)
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.services.xray_clients import XrayClientManager
from apps.keys.signals import xray_restarting
from apps.keys.tests.fake_xray import BACKUP, STAGED, FakeXrayServer
from apps.keys.tests.steps import FIXTURES, RecordedSteps

OWN_CLIENT = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"
NEW_CLIENT = "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e"


@pytest.fixture
def server() -> FakeXrayServer:
    return FakeXrayServer(
        {
            XRAY_CONFIG_PATH: (FIXTURES / "xray_server.json").read_text(encoding="utf-8"),
            XRAY_OWN_CLIENT_PATH: f"{OWN_CLIENT}\n",
        }
    )


def manager_for(server: FakeXrayServer, steps: RecordedSteps | None = None) -> XrayClientManager:
    return XrayClientManager(server.host(), steps or RecordedSteps(), "instance:7")


def client_ids(server: FakeXrayServer) -> list[str]:
    return XrayServerConfig.parse(server.files[XRAY_CONFIG_PATH]).client_ids


def clients_table(server: FakeXrayServer) -> ClientsTable:
    return ClientsTable.parse(server.files[XRAY_CLIENTS_TABLE_PATH])


def test_add_writes_config_restarts_once_and_registers(server: FakeXrayServer) -> None:
    steps = RecordedSteps()

    manager_for(server, steps).add(NEW_CLIENT, "Phone", user_id=4)

    assert client_ids(server) == [OWN_CLIENT, NEW_CLIENT]
    assert clients_table(server).user_data(NEW_CLIENT)["lamneziaUserId"] == 4
    assert server.restarts == 1
    assert BACKUP not in server.files and STAGED not in server.files
    assert steps.titles == [
        "Lock the server",
        "Read the protocol configuration",
        "Update the server configuration",
        "Update the client list",
    ]
    assert any("reconnect" in line for line in steps.lines)


def test_disable_enable_and_revoke(server: FakeXrayServer) -> None:
    manager = manager_for(server)
    manager.add(NEW_CLIENT, "Phone")

    manager.disable(NEW_CLIENT)
    assert client_ids(server) == [OWN_CLIENT]
    assert clients_table(server).name(NEW_CLIENT) == "Phone"

    manager.enable(NEW_CLIENT, "Phone")
    assert client_ids(server) == [OWN_CLIENT, NEW_CLIENT]

    manager.revoke(NEW_CLIENT)
    assert client_ids(server) == [OWN_CLIENT]
    assert NEW_CLIENT not in clients_table(server)


def test_repeating_a_change_does_not_restart(server: FakeXrayServer) -> None:
    manager_for(server).add(NEW_CLIENT, "Phone")
    steps = RecordedSteps()

    manager_for(server, steps).add(NEW_CLIENT, "Phone")

    assert server.restarts == 1
    assert steps.titles == ["Lock the server", "Read the protocol configuration"]


def test_rejected_config_leaves_the_server_untouched(server: FakeXrayServer) -> None:
    before = server.files[XRAY_CONFIG_PATH]
    server.rejects = True

    with pytest.raises(PeerError) as error:
        manager_for(server).add(NEW_CLIENT, "Phone")

    assert error.value.code == PeerErrorCode.CONFIG_REJECTED
    assert server.files[XRAY_CONFIG_PATH] == before
    assert server.restarts == 0
    assert XRAY_CLIENTS_TABLE_PATH not in server.files


def test_xray_that_does_not_start_is_rolled_back(server: FakeXrayServer) -> None:
    before = server.files[XRAY_CONFIG_PATH]
    server.starts = False

    with pytest.raises(PeerError) as error:
        manager_for(server).add(NEW_CLIENT, "Phone")

    assert error.value.code == PeerErrorCode.NOT_SERVING
    assert error.value.detail == "rolled back"
    assert server.files[XRAY_CONFIG_PATH] == before
    assert server.running and server.restarts == 2
    assert BACKUP not in server.files


def test_rollback_that_does_not_help_is_reported(server: FakeXrayServer) -> None:
    server.starts = False
    server.good_config = ""

    with pytest.raises(PeerError) as error:
        manager_for(server).add(NEW_CLIENT, "Phone")

    assert error.value.detail == "down after rollback"
    assert BACKUP in server.files


def test_interrupted_apply_is_finished_by_the_next_run(server: FakeXrayServer) -> None:
    server.files[BACKUP] = server.files[XRAY_CONFIG_PATH]

    manager_for(server).revoke(NEW_CLIENT)

    assert server.restarts == 1
    assert BACKUP not in server.files


@pytest.mark.parametrize("change", ["disable", "revoke"])
def test_own_client_of_the_app_is_protected(server: FakeXrayServer, change: str) -> None:
    with pytest.raises(PeerError) as error:
        getattr(manager_for(server), change)(OWN_CLIENT)

    assert error.value.code == PeerErrorCode.PROTECTED_CLIENT
    assert server.restarts == 0


def test_unsupported_transport_is_explained(server: FakeXrayServer) -> None:
    data = json.loads(server.files[XRAY_CONFIG_PATH])
    data["inbounds"][0]["streamSettings"]["network"] = "xhttp"
    server.files[XRAY_CONFIG_PATH] = json.dumps(data)

    with pytest.raises(PeerError) as error:
        manager_for(server).add(NEW_CLIENT, "Phone")

    assert error.value.code == PeerErrorCode.UNSUPPORTED_SETUP
    assert error.value.message


def test_server_without_own_client_file_still_works(server: FakeXrayServer) -> None:
    del server.files[XRAY_OWN_CLIENT_PATH]

    manager_for(server).add(NEW_CLIENT, "Phone")

    assert client_ids(server) == [OWN_CLIENT, NEW_CLIENT]


def test_every_change_turns_statistics_on(server: FakeXrayServer) -> None:
    manager_for(server).add(NEW_CLIENT, "Phone")

    config = XrayServerConfig.parse(server.files[XRAY_CONFIG_PATH])
    assert config.stats_enabled
    assert NEW_CLIENT not in json.dumps(json.loads(server.files[XRAY_CONFIG_PATH])["api"])


def test_rename_puts_back_statistics_the_app_dropped(server: FakeXrayServer) -> None:
    manager_for(server).add(NEW_CLIENT, "Phone")
    data = json.loads(server.files[XRAY_CONFIG_PATH])
    for name in ("stats", "api", "policy"):
        del data[name]
    server.files[XRAY_CONFIG_PATH] = json.dumps(data)

    manager_for(server).rename(NEW_CLIENT, "Laptop")

    assert XrayServerConfig.parse(server.files[XRAY_CONFIG_PATH]).stats_enabled
    assert server.restarts == 2
    manager_for(server).rename(NEW_CLIENT, "Tablet")
    assert server.restarts == 2


def test_collector_reads_the_counters_before_each_restart(server: FakeXrayServer) -> None:
    sent: list[dict[str, Any]] = []

    def receiver(sender: object, **kwargs: Any) -> None:
        sent.append({**kwargs, "restarts": server.restarts})

    xray_restarting.connect(receiver)
    try:
        manager_for(server).add(NEW_CLIENT, "Phone")
    finally:
        xray_restarting.disconnect(receiver)

    assert [(call["source"], call["restarts"]) for call in sent] == [("instance:7", 0)]


def test_failed_reading_does_not_stop_the_change(server: FakeXrayServer) -> None:
    def receiver(sender: object, **kwargs: Any) -> None:
        raise RuntimeError("collector down")

    xray_restarting.connect(receiver)
    try:
        manager_for(server).add(NEW_CLIENT, "Phone")
    finally:
        xray_restarting.disconnect(receiver)

    assert client_ids(server) == [OWN_CLIENT, NEW_CLIENT]
