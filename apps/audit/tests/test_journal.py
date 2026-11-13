from datetime import timedelta

import pytest
from axes.helpers import get_client_ip_address
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.audit.services import journal as audit

pytestmark = pytest.mark.django_db


def test_log_keeps_names_after_the_actor_and_target_are_gone() -> None:
    actor = UserFactory(username="admin1")
    target = UserFactory(username="bob")

    event = audit.log(
        "accounts.block", actor=actor, target=target, data={"reason": "spam"}, ip="198.51.100.4"
    )
    actor.delete()
    target.delete()

    event.refresh_from_db()
    assert (event.actor, event.actor_name) == (None, "admin1")
    assert (event.target_type, event.target_repr) == ("accounts.user", "bob")
    assert event.data == {"reason": "spam"}
    assert event.ip == "198.51.100.4"


def test_log_without_actor_or_target() -> None:
    event = audit.log("system.start", actor=None)

    assert (event.actor_name, event.target_type, event.target_id) == ("", "", "")


def test_client_ip_is_the_address_nginx_sets() -> None:
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="8.8.8.8")

    assert audit.client_ip(request) == "8.8.8.8"


def test_forged_entries_before_the_proxy_one_are_ignored() -> None:
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2,8.8.8.8")

    assert audit.client_ip(request) == "8.8.8.8"


@pytest.mark.parametrize("header", ["", "not-an-ip", "1.1.1.1, garbage"])
def test_without_a_valid_forwarded_address_the_socket_peer_is_used(header: str) -> None:
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR=header, REMOTE_ADDR="10.0.0.2")

    assert audit.client_ip(request) == "10.0.0.2"


def test_lockouts_use_the_same_address() -> None:
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 8.8.8.8")

    assert get_client_ip_address(request) == "8.8.8.8"


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_page_filters_events(admin_client: Client) -> None:
    alice, bob = UserFactory(username="alice"), UserFactory(username="bob")
    audit.log("keys.issue", actor=alice, target=bob)
    old = audit.log("keys.revoke", actor=bob, target=alice)
    AuditEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))

    def rows(**query: str) -> str:
        """Actions shown in the table, without the options of the filter."""
        html = admin_client.get("/audit/", query).content.decode()
        return html[html.index("<table") :]

    assert "keys.revoke" not in rows(action="keys.issue")
    assert "keys.issue" not in rows(actor="bob")
    assert "keys.revoke" not in rows(target="bob")
    since = (timezone.localdate() - timedelta(days=1)).isoformat()
    assert "keys.revoke" not in rows(since=since) and "keys.issue" in rows(since=since)


def test_page_is_for_administrators_only(client: Client) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get("/audit/").status_code == 403
