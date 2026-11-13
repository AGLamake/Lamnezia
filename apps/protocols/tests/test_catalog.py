import pytest
from django.test import Client
from django.utils import translation

from amnezia_core.xray import XRAY_CONTAINER
from apps.plans.models import Protocol
from apps.protocols.catalog import CATALOG, ContainerGroup
from apps.protocols.services.installer import installers
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

_XRAY = "XRay (VLESS Reality)"
_AWG = "AmneziaWG is a special protocol from Amnezia based on WireGuard."
_DNS = "Replace the current DNS server with your own."


def test_every_installer_takes_its_title_from_the_catalog() -> None:
    for installer in installers:
        assert installer.title == CATALOG[installer.container].title


def test_xray_is_called_vless_reality_everywhere() -> None:
    assert CATALOG[XRAY_CONTAINER].title == _XRAY
    assert installers.get(XRAY_CONTAINER).title == _XRAY
    assert Protocol(XRAY_CONTAINER).label == _XRAY


def test_descriptions_are_translated() -> None:
    with translation.override("ru"):
        assert str(ContainerGroup.VPN.label) == "Протоколы VPN"
        assert str(CATALOG["amnezia-dns"].description).startswith("Замените текущий DNS-сервер")


def test_install_page_groups_the_choices_and_describes_them(admin_client: Client) -> None:
    server = ServerFactory()

    html = admin_client.get(f"/protocols/install/{server.pk}/").content.decode()

    assert html.index("VPN protocols") < html.index("AmneziaWG 2.0") < html.index("Services")
    assert html.index("Services") < html.index("AmneziaDNS")
    assert _AWG in html
    assert _DNS in html
    assert "x-show=\"container === 'amnezia-dns'\"" in html
    assert _XRAY in html


def test_server_card_groups_the_containers_and_describes_them(admin_client: Client) -> None:
    server = ServerFactory(
        containers=[
            {"name": "amnezia-dns", "port": None, "transport": "udp"},
            {"name": "amnezia-awg2", "port": 5542, "transport": "udp"},
            {"name": "some-other", "port": 8080, "transport": "tcp"},
        ]
    )

    html = admin_client.get(f"/servers/{server.pk}/").content.decode()

    vpn, services, other = (html.index(h) for h in ("VPN protocols", "Services", "Other contain"))
    assert vpn < html.index(_AWG) < services < html.index(_DNS) < other < html.index("some-other")
