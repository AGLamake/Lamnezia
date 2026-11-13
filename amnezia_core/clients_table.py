"""Client registry of a container: /opt/amnezia/<type>/clientsTable, shared with the Amnezia app."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from amnezia_core.errors import ClientsTableError
from amnezia_core.qt_json import to_qt_json

CLIENT_ID = "clientId"
USER_DATA = "userData"
CLIENT_NAME = "clientName"
CREATION_DATE = "creationDate"
# Panel user who owns the client; the app ignores unknown userData keys.
LAMNEZIA_USER_ID = "lamneziaUserId"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_creation_date(moment: datetime) -> str:
    """Date as the app writes it with `QDateTime::toString()`: `Mon Nov 2 13:56:07 2026`."""
    weekday, month = _WEEKDAYS[moment.weekday()], _MONTHS[moment.month - 1]
    return f"{weekday} {month} {moment.day} {moment:%H:%M:%S} {moment.year}"


@dataclass(frozen=True)
class ClientRecord:
    """What clientsTable says about a client: its name in the app and the panel owner."""

    name: str
    created_at: datetime
    user_id: int | None = None


class ClientsTable:
    """Ordered list of `{"clientId": ..., "userData": {...}}` records.

    Fields the panel does not know are kept untouched; the panel adds only its own
    `userData` keys, which the app ignores.
    """

    def __init__(self, clients: list[dict[str, Any]]) -> None:
        self._clients = clients

    @classmethod
    def parse(cls, text: str) -> "ClientsTable":
        """Read the array format or the legacy `{clientId: {clientName}}` object."""
        if not text.strip():
            return cls([])
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ClientsTableError(f"clientsTable is not valid JSON: {error}") from error
        if isinstance(data, dict):
            return cls._from_legacy(data)
        if not isinstance(data, list):
            raise ClientsTableError("clientsTable must be a JSON array or object.")
        for client in data:
            if not isinstance(client, dict) or not isinstance(client.get(CLIENT_ID), str):
                raise ClientsTableError("Every clientsTable record needs a string clientId.")
        return cls(data)

    @classmethod
    def _from_legacy(cls, data: dict[str, Any]) -> "ClientsTable":
        # Same migration as upstream UsersController::migration: only clientName is carried over.
        clients = []
        for client_id, legacy in data.items():
            user_data: dict[str, Any] = {}
            if isinstance(legacy, dict) and CLIENT_NAME in legacy:
                user_data[CLIENT_NAME] = legacy[CLIENT_NAME]
            clients.append({CLIENT_ID: client_id, USER_DATA: user_data})
        return cls(clients)

    def serialize(self) -> str:
        """Always the array format, exactly as the app writes it."""
        return to_qt_json(self._clients)

    @property
    def client_ids(self) -> list[str]:
        return [client[CLIENT_ID] for client in self._clients]

    def __contains__(self, client_id: object) -> bool:
        return client_id in self.client_ids

    def __len__(self) -> int:
        return len(self._clients)

    def user_data(self, client_id: str) -> dict[str, Any]:
        """Copy of the client's userData; raises KeyError for an unknown client."""
        return dict(self._user_data(self._find(client_id)))

    def name(self, client_id: str) -> str | None:
        name = self.user_data(client_id).get(CLIENT_NAME)
        return name if isinstance(name, str) else None

    def add(
        self,
        client_id: str,
        name: str,
        created_at: datetime,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Register a client; an existing one is renamed and re-dated, as the app does."""
        client = self._find_or_none(client_id)
        if client is None:
            client = {CLIENT_ID: client_id, USER_DATA: {}}
            self._clients.append(client)
        fields = {CLIENT_NAME: name, CREATION_DATE: format_creation_date(created_at)}
        self.update(client_id, {**(extra or {}), **fields})

    def register(self, client_id: str, record: ClientRecord) -> None:
        """Add a missing client; for a known one only set the owner, keeping the app's name."""
        owner = {LAMNEZIA_USER_ID: record.user_id} if record.user_id is not None else {}
        if client_id not in self:
            self.add(client_id, record.name, record.created_at, owner)
        elif owner and self.user_data(client_id).get(LAMNEZIA_USER_ID) != record.user_id:
            self.update(client_id, owner)

    def update(self, client_id: str, fields: Mapping[str, Any]) -> None:
        """Merge fields into the client's userData (rename, panel-specific keys)."""
        client = self._find(client_id)
        client[USER_DATA] = {**self._user_data(client), **fields}

    def remove(self, client_id: str) -> bool:
        client = self._find_or_none(client_id)
        if client is None:
            return False
        self._clients.remove(client)
        return True

    def _find(self, client_id: str) -> dict[str, Any]:
        client = self._find_or_none(client_id)
        if client is None:
            raise KeyError(client_id)
        return client

    def _find_or_none(self, client_id: str) -> dict[str, Any] | None:
        return next((c for c in self._clients if c[CLIENT_ID] == client_id), None)

    @staticmethod
    def _user_data(client: dict[str, Any]) -> dict[str, Any]:
        user_data = client.get(USER_DATA)
        return user_data if isinstance(user_data, dict) else {}
