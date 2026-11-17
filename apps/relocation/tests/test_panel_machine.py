from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.relocation.forms import MoveForm
from apps.relocation.models import PanelMove
from apps.relocation.services.panel_machine import PanelMachine
from apps.servers.tests.factories import ServerFactory

PANEL = PanelMachine(["panel.example.com", "203.0.113.30:8443"])


@pytest.mark.parametrize(
    "host",
    [
        "panel.example.com",  # the panel's domain
        "Panel.Example.com.",  # the same name, other spelling
        "203.0.113.10",  # the external IP the domain points to
        "alias.example.net",  # another name of that IP
        "203.0.113.30",  # the host the admin opened the panel at
    ],
)
def test_the_panel_machine_is_recognised(host: str) -> None:
    assert PANEL.includes(host)


@pytest.mark.parametrize("host", ["other.example.org", "203.0.113.20", "unknown.example", ""])
def test_another_machine_is_not_the_panel(host: str) -> None:
    assert not PANEL.includes(host)


def test_the_panel_hosts_come_from_site_url_and_the_request(settings: Any) -> None:
    settings.SITE_URL = "https://panel.example.com"
    panel = PanelMachine.current("[2001:db8::5]:443")
    assert panel.includes("203.0.113.10")
    assert panel.includes("2001:db8::5")
    assert not panel.includes("203.0.113.20")


@pytest.mark.django_db
def test_the_form_refuses_the_machine_of_the_panel() -> None:
    server = ServerFactory(host="203.0.113.10")
    form = MoveForm({"target": server.pk, "understood": "on"}, panel=PANEL)
    assert not form.is_valid()
    assert "203.0.113.10" in form.errors["target"][0]

    other = ServerFactory(host="other.example.org")
    assert MoveForm({"target": other.pk, "understood": "on"}, panel=PANEL).is_valid()


@pytest.mark.django_db
def test_the_page_does_not_start_a_move_to_itself(client: Client, settings: Any) -> None:
    settings.SITE_URL = "https://panel.example.com"
    client.force_login(UserFactory(role=Role.ADMIN))
    server = ServerFactory(host="alias.example.net")
    response = client.post("/backups/move/", {"target": server.pk, "understood": "on"})
    assert response.status_code == 200
    assert not PanelMove.objects.exists()
