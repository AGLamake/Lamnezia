import pytest
from django.test import Client

from amnezia_core.awg_install import upgraded_awg_params
from apps.accounts.models import User
from apps.keys.models import AccessKey
from apps.keys.tests.test_views import issued_key
from apps.portal.services.app_versions import awg3_sources, needs_awg3_app
from apps.portal.tests.test_portal import subscriber
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import APP_CONFIG
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

AWG2_PARAMS = APP_CONFIG.awg_params
AWG3_PARAMS = upgraded_awg_params(AWG2_PARAMS)


@pytest.fixture
def user() -> User:
    return subscriber(max_keys=5)


def key_on(user: User, params: dict[str, str], container: str = "amnezia-awg2") -> AccessKey:
    instance = ProtocolInstance.objects.create(
        server=ServerFactory(), container=container, port=55424, params=params
    )
    return issued_key(user=user, source=instance.source, public_key=f"pk-{instance.pk}")


def test_only_keys_of_awg_3_1_need_a_new_app(user: User) -> None:
    awg3 = key_on(user, AWG3_PARAMS)
    awg2 = key_on(user, AWG2_PARAMS)
    xray = key_on(user, {}, container="amnezia-xray")
    other = issued_key(user=user, source="static:1", public_key="static")

    assert awg3_sources([awg3, awg2, xray, other]) == {awg3.source}
    assert needs_awg3_app(awg3)
    assert not needs_awg3_app(awg2)


def test_the_key_page_lists_the_app_versions(client: Client, user: User) -> None:
    client.force_login(user)
    awg3, awg2 = key_on(user, AWG3_PARAMS), key_on(user, AWG2_PARAMS)

    assert "5.0.1.5" in client.get(f"/portal/keys/{awg3.pk}/").content.decode()
    assert "5.0.1.5" not in client.get(f"/portal/keys/{awg2.pk}/").content.decode()
