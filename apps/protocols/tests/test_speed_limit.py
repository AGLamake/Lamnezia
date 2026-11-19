from unittest.mock import MagicMock

import pytest
from django.template import Context, Template
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.groups.models import GroupMember, ServerGroup
from apps.protocols.models import ProtocolInstance
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


@pytest.fixture
def delay(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("apps.traffic.tasks.apply_speed_limits.delay", mock)
    return mock


def post(client: Client, instance: ProtocolInstance, **data: str) -> int:
    return client.post(f"/protocols/{instance.pk}/speed-limit/", data).status_code


def test_settings_page_has_the_speed_limit_of_the_server(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    instance.download_limit = 300
    instance.save()

    html = admin_client.get(f"/protocols/{instance.pk}/settings/").content.decode()

    assert f'action="/protocols/{instance.pk}/speed-limit/"' in html
    assert 'name="download_limit" value="300"' in html


def test_saved_limit_is_audited_and_applied(
    admin_client: Client,
    instance: ProtocolInstance,
    delay: MagicMock,
    django_capture_on_commit_callbacks: object,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):  # type: ignore[operator]
        assert post(admin_client, instance, download_limit="100", upload_limit="") == 302

    instance.refresh_from_db()
    assert (instance.download_limit, instance.upload_limit) == (100, None)
    event = AuditEvent.objects.get(action="protocols.speed_limit")
    assert event.data == {"download": 100, "upload": None}
    delay.assert_called_once_with()


def test_unchanged_limit_starts_nothing(
    admin_client: Client, instance: ProtocolInstance, delay: MagicMock
) -> None:
    post(admin_client, instance, download_limit="", upload_limit="")

    assert not AuditEvent.objects.filter(action="protocols.speed_limit").exists()
    delay.assert_not_called()


def test_wrong_limit_is_refused(
    admin_client: Client, instance: ProtocolInstance, delay: MagicMock
) -> None:
    post(admin_client, instance, download_limit="0", upload_limit="")

    instance.refresh_from_db()
    assert instance.download_limit is None
    delay.assert_not_called()


def test_group_clone_has_no_limit_of_its_own(
    admin_client: Client, instance: ProtocolInstance, delay: MagicMock
) -> None:
    GroupMember.objects.create(
        group=ServerGroup.objects.create(name="Europe", port=instance.port),
        instance=instance,
        priority=2,
    )

    assert post(admin_client, instance, download_limit="100") == 404
    html = admin_client.get(f"/protocols/{instance.pk}/settings/").content.decode()
    assert "/speed-limit/" not in html


def test_limit_is_for_wireguard_family_only(admin_client: Client) -> None:
    xray = ProtocolInstance.objects.create(
        server=ServerFactory(), container="amnezia-xray", port=443
    )

    assert post(admin_client, xray, download_limit="100") == 404


def test_admins_only(client: Client, instance: ProtocolInstance, delay: MagicMock) -> None:
    post(client, instance, download_limit="100")

    instance.refresh_from_db()
    assert instance.download_limit is None


def render_line(instance: ProtocolInstance) -> str:
    template = Template("{% load speed_limit %}{% speed_limit_line instance %}")
    return template.render(Context({"instance": instance}))


def test_server_card_shows_the_limit_and_the_missing_ifb(instance: ProtocolInstance) -> None:
    assert render_line(instance).strip() == ""

    instance.download_limit, instance.upload_limit = 100, 20
    TrafficSource.objects.create(
        source=instance.source, upload_policed=True, checked_at=timezone.now()
    )
    html = render_line(instance)

    assert "download 100 Mbit/s, upload 20 Mbit/s" in html
    assert "No ifb module on the server" in html
