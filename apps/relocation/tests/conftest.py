"""Fixtures of the panel move tests: no real DNS lookups."""

import pytest

from apps.relocation.services import panel_machine

FAKE_DNS = {
    "panel.example.com": {"203.0.113.10"},
    "alias.example.net": {"203.0.113.10"},
    "other.example.org": {"203.0.113.20"},
    "localhost": {"127.0.0.1", "::1"},
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    monkeypatch.setattr(panel_machine, "resolve", lambda name: FAKE_DNS.get(name, set()))
    return FAKE_DNS
