"""Backup codes of the second factor: issued at setup and on request, each signs in once."""

import pytest
from django.test import Client
from django_otp.plugins.otp_static.models import StaticDevice

from apps.accounts.models import Role
from apps.accounts.services.backup_codes import BACKUP_CODE_COUNT, BackupCodes
from apps.accounts.services.two_factor import TwoFactor
from apps.accounts.tests.factories import TOTPDeviceFactory, UserFactory, current_code
from apps.accounts.tests.test_two_factor import SETUP, VERIFY, signed_in, verified, wrong_code
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

CODES = "/accounts/2fa/backup-codes/"


class TestService:
    def test_new_set_revokes_the_old_one(self) -> None:
        user = UserFactory()
        old = BackupCodes(user).issue()

        new = BackupCodes(user).issue()

        assert len(new) == BACKUP_CODE_COUNT == len(set(new))
        assert BackupCodes(user).use(new[0]) is not None
        assert BackupCodes(user).use(old[0]) is None
        assert StaticDevice.objects.count() == 1

    def test_code_works_once(self) -> None:
        user = UserFactory()
        code = BackupCodes(user).issue()[0]

        assert BackupCodes(user).use(code) is not None
        assert BackupCodes(user).use(code) is None
        assert BackupCodes(user).left == BACKUP_CODE_COUNT - 1

    def test_code_of_another_user_does_not_work(self) -> None:
        code = BackupCodes(UserFactory()).issue()[0]

        assert BackupCodes(UserFactory()).use(code) is None

    def test_turning_2fa_off_removes_the_codes(self) -> None:
        device = TOTPDeviceFactory()
        BackupCodes(device.user).issue()

        TwoFactor(device.user).disable()

        assert BackupCodes(device.user).left == 0

    def test_codes_alone_do_not_turn_2fa_on(self) -> None:
        user = UserFactory()
        BackupCodes(user).issue()

        assert not TwoFactor(user).enabled


class TestSignIn:
    def test_backup_code_signs_in_once(self, client: Client) -> None:
        device = TOTPDeviceFactory(user__role=Role.MODERATOR)
        code = BackupCodes(device.user).issue()[0]
        signed_in(client, device.user)

        response = client.post(VERIFY, {"token": code.upper(), "next": "/dashboard/"})

        assert response["Location"] == "/dashboard/"
        assert client.get("/dashboard/").status_code == 200
        event = AuditEvent.objects.get(action="accounts.backup_code_use")
        assert event.actor == device.user
        assert event.data == {"left": BACKUP_CODE_COUNT - 1}
        assert code not in str(event.data)

        client.logout()
        signed_in(client, device.user)
        assert client.post(VERIFY, {"token": code}).status_code == 200
        assert client.get("/dashboard/")["Location"].startswith(VERIFY)

    def test_code_from_the_app_still_works(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        BackupCodes(device.user).issue()
        signed_in(client, device.user)

        response = client.post(VERIFY, {"token": current_code(device), "next": "/portal/"})

        assert response["Location"] == "/portal/"
        assert not AuditEvent.objects.filter(action="accounts.backup_code_use").exists()


class TestPages:
    def test_setup_shows_the_codes_once(self, client: Client) -> None:
        user = UserFactory(role=Role.ADMIN)
        signed_in(client, user)
        client.get(SETUP)
        device = TwoFactor(user)._pending_device()

        page = client.post(SETUP, {"token": current_code(device), "next": "/admin/"})

        assert BackupCodes(user).left == BACKUP_CODE_COUNT
        assert AuditEvent.objects.filter(action="accounts.backup_codes_issue").count() == 1
        codes_page = page.content.decode()
        assert "The codes are shown only now." in codes_page
        assert "backup codes are left" in client.get(CODES).content.decode()

    def test_wrong_code_from_the_app_keeps_the_old_codes(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        old = BackupCodes(device.user).issue()
        verified(client, device)

        page = client.post(CODES, {"token": wrong_code(device)}).content.decode()

        assert "The codes are shown only now." not in page
        assert BackupCodes(device.user).use(old[0]) is not None
        assert not AuditEvent.objects.filter(action="accounts.backup_codes_issue").exists()

    def test_new_codes_revoke_the_old_ones(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        old = BackupCodes(device.user).issue()
        verified(client, device)

        page = client.post(CODES, {"token": current_code(device)}).content.decode()

        assert "The codes are shown only now." in page
        assert old[0] not in page
        assert BackupCodes(device.user).left == BACKUP_CODE_COUNT
        assert BackupCodes(device.user).use(old[0]) is None
        event = AuditEvent.objects.get(action="accounts.backup_codes_issue")
        assert event.actor == device.user

    def test_no_codes_without_an_authenticator(self, client: Client) -> None:
        signed_in(client, UserFactory(role=Role.MODERATOR))

        assert client.get(CODES)["Location"] == "/accounts/profile/#two-factor"

    def test_the_profile_counts_the_codes(self, client: Client) -> None:
        device = TOTPDeviceFactory()
        BackupCodes(device.user).issue()
        verified(client, device)

        page = client.get("/accounts/profile/").content.decode()

        assert f"{BACKUP_CODE_COUNT} backup codes are left" in page
        assert CODES in page
