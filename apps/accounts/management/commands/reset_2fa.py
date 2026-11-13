from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.accounts.services.two_factor import TwoFactor


class Command(BaseCommand):
    help = "Remove a user's authenticator app, e.g. when the phone is lost."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, **options: Any) -> None:
        username = options["username"]
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"User {username} does not exist.")
        TwoFactor(user).disable()
        self.stdout.write(f"Two-factor login of {username} is reset.")
