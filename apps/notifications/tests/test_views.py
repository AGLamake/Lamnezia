from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure as configure_site
from apps.audit.models import AuditEvent
from apps.notifications.events import Event
from apps.notifications.models import Channel, Delivery, DeliveryStatus
from apps.notifications.services.channels import DeliveryError
from apps.notifications.services.delivery import emit
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def webhook_data(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Monitoring",
        "target": "https://hooks.example.com/lamnezia",
        "secret": "s3cret",
        "enabled": "on",
        "events": [Event.SERVER_DOWN, Event.LIMIT_REACHED],
        **overrides,
    }


@pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
def test_notifications_are_for_admins_only(client: Client, role: Role) -> None:
    client.force_login(UserFactory(role=role))

    assert client.get(reverse("notifications:channels")).status_code == 403
    assert client.get(reverse("notifications:log")).status_code == 403


def test_the_list_shows_which_channels_hear_each_event(admin_client: Client) -> None:
    channel = ChannelFactory(name="Ops chat", events=[Event.SERVER_DOWN])

    response = admin_client.get(reverse("notifications:channels"))

    assert response.status_code == 200
    assert "Ops chat" in response.content.decode()
    assert [channels for _label, channels in response.context["routing"] if channels] == [[channel]]


def test_create_a_webhook_channel_with_its_events(admin_client: Client) -> None:
    response = admin_client.post(reverse("notifications:add", args=["webhook"]), webhook_data())

    assert response.status_code == 302
    channel = Channel.objects.get()
    assert (channel.kind, channel.secret) == ("webhook", "s3cret")
    assert sorted(channel.routes.values_list("event", flat=True)) == [
        "limit_reached",
        "server_down",
    ]
    assert AuditEvent.objects.get().action == "notifications.channel_create"


def test_a_webhook_needs_a_signing_secret(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("notifications:add", args=["webhook"]), webhook_data(secret="")
    )

    assert response.status_code == 200
    assert "secret" in response.context["form"].errors
    assert not Channel.objects.exists()


@pytest.mark.parametrize("target", ["http://127.0.0.1:8000/hook", "http://10.0.0.5/hook"])
def test_a_webhook_into_a_private_network_is_refused_until_allowed(
    admin_client: Client, target: str
) -> None:
    url = reverse("notifications:add", args=["webhook"])

    refused = admin_client.post(url, webhook_data(target=target))

    assert "private network" in str(refused.context["form"].errors["target"])
    assert not Channel.objects.exists()

    configure_site(webhooks_private_networks=True)
    assert admin_client.post(url, webhook_data(target=target)).status_code == 302
    assert Channel.objects.get().target == target


def test_a_discord_webhook_into_a_private_network_is_refused(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("notifications:add", args=["discord"]),
        {"name": "Chat", "secret": "http://192.168.1.10/api/webhooks/1/x", "enabled": "on"},
    )

    assert "secret" in response.context["form"].errors
    assert not Channel.objects.exists()


def test_an_unknown_kind_is_not_found(admin_client: Client) -> None:
    assert admin_client.get(reverse("notifications:add", args=["pigeon"])).status_code == 404


def test_the_saved_secret_is_not_shown_and_kept_when_left_empty(admin_client: Client) -> None:
    channel = ChannelFactory(secret="s3cret", events=[Event.SERVER_DOWN])
    url = reverse("notifications:edit", args=[channel.pk])

    assert "s3cret" not in admin_client.get(url).content.decode()
    response = admin_client.post(url, webhook_data(name="Renamed", secret="", events=[]))

    assert response.status_code == 302
    channel.refresh_from_db()
    assert (channel.name, channel.secret) == ("Renamed", "s3cret")
    assert not channel.routes.exists()
    audit = AuditEvent.objects.get()
    assert "s3cret" not in str(audit.data)


def test_email_channel_checks_the_addresses(admin_client: Client) -> None:
    url = reverse("notifications:add", args=["email"])
    data = {"name": "Admins", "events": [Event.SERVER_DOWN], "enabled": "on"}

    bad = admin_client.post(url, {**data, "target": "a@example.com, not-an-address"})
    good = admin_client.post(url, {**data, "target": "a@example.com;b@example.com"})

    assert "target" in bad.context["form"].errors
    assert good.status_code == 302
    assert Channel.objects.get().target == "a@example.com, b@example.com"


def test_the_test_button_sends_a_message_and_opens_the_log(
    admin_client: Client, http: FakeHttp, commit: None
) -> None:
    channel = ChannelFactory()

    response = admin_client.post(reverse("notifications:test", args=[channel.pk]))

    assert response["Location"] == reverse("notifications:log")
    assert Delivery.objects.get().status == DeliveryStatus.SENT
    assert [post.url for post in http.posts] == [channel.target]


def test_the_log_filters_by_status(admin_client: Client, http: FakeHttp, commit: None) -> None:
    broken = ChannelFactory(events=[Event.SERVER_DOWN])
    ChannelFactory(events=[Event.SERVER_DOWN])
    http.failing[broken.target] = DeliveryError("HTTP 404 not found", retryable=False)
    emit(Event.SERVER_DOWN, {"server": "Germany"})

    everything = admin_client.get(reverse("notifications:log"))
    failed = admin_client.get(reverse("notifications:log"), {"status": DeliveryStatus.FAILED})

    assert len(everything.context["deliveries"]) == 2
    [delivery] = failed.context["deliveries"]
    assert delivery.channel == broken
    assert "HTTP 404 not found" in failed.content.decode()


def test_delete_a_channel(admin_client: Client) -> None:
    channel = ChannelFactory()

    response = admin_client.post(reverse("notifications:delete", args=[channel.pk]))

    assert response.status_code == 302
    assert not Channel.objects.exists()
