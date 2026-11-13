"""The seed_demo command: fills the database with made-up data for screenshots and manual checks."""

from argparse import ArgumentParser
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.services.demo import PASSWORD, USERS, DemoSeed


class Command(BaseCommand):
    help = "Fill the database with demo servers, users, keys, traffic, tickets and notifications."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--force", action="store_true", help="Run even with DEBUG off (never on production)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG and not options["force"]:
            raise CommandError("DEBUG is off. Demo data is for development; pass --force to seed.")
        DemoSeed().run()
        logins = ", ".join(["admin", "moderator", *(username for username, _, _ in USERS)])
        self.stdout.write(f"Demo data is ready. Logins: {logins}; password: {PASSWORD}")
