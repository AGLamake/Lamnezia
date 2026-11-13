from typing import Any

import pytest
from django.test import Client, override_settings
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Role, User
from apps.accounts.services.two_factor import TwoFactor
from apps.accounts.tests.factories import TOTPDeviceFactory, UserFactory, current_code

pytestmark = pytest.mark.django_db

VERIFY = "/accounts/2fa/verify/"
SETUP = "/accounts/2fa/setup/"


def signed_in(client: Client, user: User) -> Client:
    client.force_login(user)
    return client


def verified(client: Client, device: TOTPDevice) -> Client:
    """Signed in and past the code prompt."""
    signed_in(client, device.user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client


def wrong_code(device: TOTPDevice) -> str:
    return "123456" if current_code(device) != "123456" else "654321"


class TestService:
    def test_setup_is_confirmed_by_the_first_code(self) -> None:
        two_factor = TwoFactor(UserFactory())
        enrollment = two_factor.enrollment()
        device = TOTPDevice.objects.get(confirmed=False)

        assert enrollment.qr_data_uri.startswith("data:image/svg+xml")
        assert two_factor.confirm(current_code(device)) == device
        assert two_factor.enabled

    def test_wrong_code_does_not_turn_it_on(self) -> None:
        two_factor = TwoFactor(UserFactory())
        two_factor.enrollment()

        assert two_factor.confirm(wrong_code(TOTPDevice.objects.get())) is None
        assert not two_factor.enabled

    def test_enrollment_keeps_its_secret_until_confirmed(self) -> None:
        two_factor = TwoFactor(UserFactory())

        assert two_factor.enrollment().secret == two_factor.enrollment().secret

    def test_new_device_replaces_the_old_one(self) -> None:
        old = TOTPDeviceFactory()
        two_factor = TwoFactor(old.user)
        two_factor.enrollment()
        new = TOTPDevice.objects.get(confirmed=False)

        two_factor.confirm(current_code(new))

        assert list(TOTPDevice.objects.filter(user=old.user)) == [new]

    def test_code_is_checked_once(self) -> None:
        device = TOTPDeviceFactory()
        code = current_code(device)

        assert TwoFactor(device.user).verify(code) == device
        assert TwoFactor(device.user).verify(code) is None

    def test_disable_removes_the_device(self) -> None:
        device = TOTPDeviceFactory()

        TwoFactor(device.user).disable()

        assert not TwoFactor(device.user).enabled

    @pytest.mark.parametrize(
        ("role", "setting", "required"),
        [(Role.ADMIN, True, True), (Role.ADMIN, False, False), (Role.MODERATOR, True, False)],
    )
    def test_required_for_admins_by_setting(
        self, role: Role, setting: bool, required: bool
    ) -> None:
        with override_settings(REQUIRE_ADMIN_2FA=setting):
            assert TwoFactor(UserFactory(role=role)).required is required


class TestRequiredForAdmins:
    @pytest.fixture(autouse=True)
    def _required(self, settings: Any) -> None:
        settings.REQUIRE_ADMIN_2FA = True

    def test_admin_without_2fa_does_not_get_into_the_admin_site(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.ADMIN))

        response = client.get("/admin/")

        assert response.status_code == 302
        assert response["Location"] == f"{SETUP}?next=/admin/"

    def test_admin_without_2fa_is_held_on_every_page(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.ADMIN))

        assert client.get("/dashboard/")["Location"].startswith(SETUP)
        assert client.get(SETUP).status_code == 200

    def test_admin_gets_in_after_setting_it_up(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.ADMIN))
        client.get(SETUP)
        code = current_code(TOTPDevice.objects.get())

        response = client.post(SETUP, {"token": code, "next": "/admin/"})

        assert response.status_code == 200
        assert 'href="/admin/"' in response.content.decode()
        assert client.get("/admin/").status_code == 200

    @override_settings(REQUIRE_ADMIN_2FA=False)
    def test_setting_off_lets_admin_in(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.ADMIN))

        assert client.get("/admin/").status_code == 200

    def test_moderator_may_go_without_it(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.MODERATOR))

        assert client.get("/dashboard/").status_code == 200


class TestSignIn:
    def test_user_with_2fa_enters_a_code_after_the_password(self, client: Client) -> None:
        device = TOTPDeviceFactory(user__role=Role.MODERATOR)
        signed_in(client, device.user)

        assert client.get("/dashboard/")["Location"] == f"{VERIFY}?next=/dashboard/"

        response = client.post(VERIFY, {"token": current_code(device), "next": "/dashboard/"})

        assert response["Location"] == "/dashboard/"
        assert client.get("/dashboard/").status_code == 200

    def test_wrong_code_keeps_the_user_out(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        signed_in(client, device.user)

        response = client.post(VERIFY, {"token": wrong_code(device)})

        assert response.status_code == 200
        assert client.get("/portal/")["Location"].startswith(VERIFY)

    def test_password_alone_cannot_replace_the_authenticator(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        signed_in(client, device.user)

        assert client.get(SETUP)["Location"].startswith(VERIFY)
        assert client.post(SETUP, {"token": "123456"})["Location"].startswith(VERIFY)

    def test_logout_works_before_the_code(self, client: Client) -> None:
        signed_in(client, TOTPDeviceFactory().user)

        client.post("/accounts/logout/")

        assert "_auth_user_id" not in client.session

    def test_next_must_stay_on_the_site(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        signed_in(client, device.user)

        response = client.post(
            VERIFY, {"token": current_code(device), "next": "https://evil.test/"}
        )

        assert response["Location"] == "/"


class TestDisable:
    def test_user_turns_it_off_with_a_code(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        verified(client, device)

        client.post("/accounts/2fa/disable/", {"token": current_code(device)})

        assert not TwoFactor(device.user).enabled

    @override_settings(REQUIRE_ADMIN_2FA=True)
    def test_admin_cannot_turn_it_off(self, client: Client) -> None:
        device = TOTPDeviceFactory(user__role=Role.ADMIN)
        verified(client, device)

        response = client.post("/accounts/2fa/disable/", {"token": current_code(device)})

        assert response["Location"] == "/accounts/profile/#two-factor"
        assert TwoFactor(device.user).enabled
