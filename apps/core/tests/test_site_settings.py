from typing import Any

import pytest
from django.db import connection
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.core.models import BotKeyDelivery, KeyRevealPolicy, RegistrationMode, SiteSettings
from apps.core.services.site_settings import SettingsCache, cached_settings

pytestmark = pytest.mark.django_db

PAGE = "/settings/"


def test_defaults() -> None:
    site = SiteSettings.load()

    assert site.registration_mode == RegistrationMode.INVITE_ONLY
    assert site.key_reveal_policy == KeyRevealPolicy.ALWAYS
    assert site.bot_key_delivery == BotKeyDelivery.ALLOWED
    assert site.require_email_confirmation


def test_there_is_one_row() -> None:
    SiteSettings(site_name="A").save()
    SiteSettings(site_name="B").save()

    assert SiteSettings.objects.get().site_name == "B"


def test_secrets_are_encrypted_in_the_database() -> None:
    site = SiteSettings.load()
    site.github_token = "ghp_secret"
    site.save()

    with connection.cursor() as cursor:
        cursor.execute("SELECT github_token FROM core_sitesettings")
        (stored,) = cursor.fetchone()
    assert "ghp_secret" not in stored
    assert SiteSettings.load().github_token == "ghp_secret"


class TestCache:
    def test_reading_again_does_not_query_the_database(
        self, django_assert_num_queries: Any
    ) -> None:
        settings_cache = SettingsCache()
        settings_cache.get()

        with django_assert_num_queries(0):
            settings_cache.get()

    def test_change_is_seen_after_saving(self, django_capture_on_commit_callbacks: Any) -> None:
        settings_cache = SettingsCache()
        settings_cache.get()

        with django_capture_on_commit_callbacks(execute=True):
            SiteSettings(site_name="My VPN").save()

        assert settings_cache.get().site_name == "My VPN"

    def test_another_process_sees_the_change(self) -> None:
        reader, writer = SettingsCache(), SettingsCache()
        reader.get()
        SiteSettings(site_name="My VPN").save()

        writer.invalidate()

        assert reader.get().site_name == "My VPN"


@pytest.mark.parametrize(
    ("role", "status"), [(None, 302), (Role.USER, 403), (Role.MODERATOR, 403), (Role.ADMIN, 200)]
)
def test_page_is_for_admins(client: Client, role: Role | None, status: int) -> None:
    if role is not None:
        client.force_login(UserFactory(role=role))

    assert client.get(PAGE).status_code == status


class TestPage:
    @pytest.fixture
    def admin(self, client: Client) -> Client:
        client.force_login(UserFactory(role=Role.ADMIN))
        return client

    def form(self, **changes: str) -> dict[str, str]:
        data = {
            "site_name": "Lamnezia",
            "registration_mode": "invite_only",
            "require_email_confirmation": "on",
            "key_reveal_policy": "always",
            "bot_key_delivery": "allowed",
            "guide_suggestions": "on",
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_security": "starttls",
            "smtp_username": "",
            "from_email": "",
            "smtp_password": "",
            "telegram_bot_token": "",
            "github_token": "",
        }
        return {**data, **changes}

    def test_saves_the_settings(self, admin: Client) -> None:
        response = admin.post(PAGE, self.form(site_name="My VPN", registration_mode="open_auto"))

        assert response["Location"] == PAGE
        site = SiteSettings.load()
        assert (site.site_name, site.registration_mode) == ("My VPN", "open_auto")

    def test_telegram_sign_in_tells_to_set_the_panel_domain_in_botfather(
        self, admin: Client, settings: Any
    ) -> None:
        settings.ALLOWED_HOSTS = ["panel.example"]

        page = admin.get(PAGE, HTTP_HOST="panel.example:8443").content.decode()

        assert "<code>/setdomain</code>" in page
        assert '<code class="break-all">panel.example</code>' in page

    def test_webhooks_to_private_networks_are_off_until_switched_on(self, admin: Client) -> None:
        assert not SiteSettings.load().webhooks_private_networks

        admin.post(PAGE, self.form(webhooks_private_networks="on"))

        assert SiteSettings.load().webhooks_private_networks

    def test_saved_secret_is_not_shown(self, admin: Client) -> None:
        admin.post(PAGE, self.form(github_token="ghp_secret"))

        html = admin.get(PAGE).content.decode()

        assert "ghp_secret" not in html
        assert SiteSettings.load().github_token == "ghp_secret"

    def test_empty_secret_keeps_the_saved_one(self, admin: Client) -> None:
        admin.post(PAGE, self.form(telegram_bot_token="123:abc"))

        admin.post(PAGE, self.form(site_name="Other"))

        assert SiteSettings.load().telegram_bot_token == "123:abc"

    def test_checkbox_removes_the_secret(self, admin: Client) -> None:
        admin.post(PAGE, self.form(telegram_bot_token="123:abc"))

        admin.post(PAGE, self.form(clear_telegram_bot_token="on"))

        assert SiteSettings.load().telegram_bot_token == ""

    @pytest.mark.parametrize(
        "typed", ["lamnezia_bot", "@lamnezia_bot", "https://t.me/lamnezia_bot"]
    )
    def test_the_bot_username_is_kept_bare_for_t_me_links(self, admin: Client, typed: str) -> None:
        admin.post(PAGE, self.form(telegram_bot_username=typed))

        assert SiteSettings.load().telegram_bot_username == "lamnezia_bot"

    def test_a_bot_username_with_other_characters_is_refused(self, admin: Client) -> None:
        response = admin.post(PAGE, self.form(telegram_bot_username="my bot"))

        assert response.status_code == 200
        assert SiteSettings.load().telegram_bot_username == ""

    def test_name_is_shown_in_the_layout(self, admin: Client) -> None:
        admin.post(PAGE, self.form(site_name="My VPN"))
        cached_settings.invalidate()

        assert "My VPN" in admin.get("/dashboard/").content.decode()

    def test_change_is_logged_without_values(self, admin: Client) -> None:
        admin.post(PAGE, self.form(site_name="My VPN", github_token="ghp_secret"))

        event = AuditEvent.objects.get(action="core.settings_change")
        assert event.data == {"fields": ["github_token", "site_name"]}
        assert "ghp_secret" not in str(event.data)
