from typing import Any

from django.core.management.base import BaseCommand

from apps.ssh.services.rotation import SecretRotation


class Command(BaseCommand):
    help = (
        "Re-encrypt all stored secrets with the first key of LAMNEZIA_ENCRYPTION_KEY. "
        "Afterwards the old keys can be removed from the setting."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        rows = SecretRotation().run()
        self.stdout.write(f"{rows} records re-encrypted with the first key.")
