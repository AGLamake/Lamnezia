import sys
from argparse import ArgumentParser
from getpass import getpass
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role, User


class Command(BaseCommand):
    help = "Create a panel administrator. Asks for the password unless --password-stdin is given."
    stealth_options = ("stdin",)

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="Read the password from standard input, for scripts.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        username = options["username"]
        if User.objects.filter(username=username).exists():
            raise CommandError(f"User {username} already exists.")
        user = User(username=username, email=options["email"], role=Role.ADMIN)
        if options["password_stdin"]:
            password = (options.get("stdin") or sys.stdin).readline().rstrip("\r\n")
        else:
            password = self._ask_password()
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        user.set_password(password)
        user.save()
        self.stdout.write(f"Administrator {username} created. Sign in to set up two-factor login.")

    @staticmethod
    def _ask_password() -> str:
        password = getpass("Password: ")
        if password != getpass("Password (again): "):
            raise CommandError("The passwords do not match.")
        return password
