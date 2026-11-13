from collections.abc import Callable
from datetime import datetime

import pytest

from amnezia_core.clients_table import ClientsTable, format_creation_date
from amnezia_core.errors import ClientsTableError
from amnezia_core.tests.conftest import FIRST_PEER, SECOND_PEER

CREATED = datetime(2026, 11, 2, 9, 5, 7)


@pytest.fixture
def table(read_fixture: Callable[[str], str]) -> ClientsTable:
    return ClientsTable.parse(read_fixture("clientsTable.json"))


def test_round_trip_keeps_file_written_by_app(read_fixture: Callable[[str], str]) -> None:
    text = read_fixture("clientsTable.json")

    assert ClientsTable.parse(text).serialize() == text


def test_reads_clients(table: ClientsTable) -> None:
    assert table.client_ids == [FIRST_PEER, SECOND_PEER]
    assert len(table) == 2
    assert FIRST_PEER in table
    assert table.name(FIRST_PEER) == "Ноутбук Анны"
    assert table.user_data(FIRST_PEER)["dataSent"] == "15.40 MiB"


def test_legacy_object_is_migrated_like_upstream(read_fixture: Callable[[str], str]) -> None:
    table = ClientsTable.parse(read_fixture("clientsTable_legacy.json"))

    assert table.serialize() == (
        "[\n"
        "    {\n"
        f'        "clientId": "{FIRST_PEER}",\n'
        '        "userData": {\n'
        '            "clientName": "Phone"\n'
        "        }\n"
        "    }\n"
        "]\n"
    )


def test_legacy_record_without_name_gets_empty_user_data() -> None:
    table = ClientsTable.parse('{"k": 1}')

    assert table.user_data("k") == {}
    assert table.name("k") is None


@pytest.mark.parametrize("text", ["", "  \n"])
def test_empty_file_is_empty_table(text: str) -> None:
    table = ClientsTable.parse(text)

    assert len(table) == 0
    assert table.serialize() == "[\n]\n"


@pytest.mark.parametrize("text", ["{broken", "42", '[{"userData": {}}]', '["x"]'])
def test_rejects_invalid_table(text: str) -> None:
    with pytest.raises(ClientsTableError):
        ClientsTable.parse(text)


def test_add_new_client_with_panel_fields(table: ClientsTable) -> None:
    table.add("new", "Phone", CREATED, extra={"lamneziaUserId": 7})

    assert table.client_ids[-1] == "new"
    assert table.user_data("new") == {
        "clientName": "Phone",
        "creationDate": "Mon Nov 2 09:05:07 2026",
        "lamneziaUserId": 7,
    }


def test_add_existing_client_renames_and_keeps_other_fields(table: ClientsTable) -> None:
    table.add(FIRST_PEER, "Renamed", CREATED)

    assert table.client_ids == [FIRST_PEER, SECOND_PEER]
    assert table.name(FIRST_PEER) == "Renamed"
    assert table.user_data(FIRST_PEER)["dataSent"] == "15.40 MiB"


def test_update_and_remove(table: ClientsTable) -> None:
    table.update(SECOND_PEER, {"lamneziaUserId": 3})

    assert table.user_data(SECOND_PEER)["lamneziaUserId"] == 3
    assert table.remove(SECOND_PEER) is True
    assert table.remove(SECOND_PEER) is False
    with pytest.raises(KeyError):
        table.user_data(SECOND_PEER)


def test_record_without_user_data_is_tolerated() -> None:
    table = ClientsTable.parse('[{"clientId": "k", "extra": true}]')
    table.update("k", {"clientName": "A"})

    assert table.serialize() == (
        '[\n    {\n        "clientId": "k",\n        "extra": true,\n'
        '        "userData": {\n            "clientName": "A"\n        }\n    }\n]\n'
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 1, 31, 23, 0, 1), "Sat Jan 31 23:00:01 2026"),
        (datetime(2026, 12, 6, 0, 0, 0), "Sun Dec 6 00:00:00 2026"),
    ],
)
def test_creation_date_matches_qt_text_date(moment: datetime, expected: str) -> None:
    assert format_creation_date(moment) == expected
