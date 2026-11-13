"""Command that adds the built-in FAQ articles and useful materials this panel does not have yet."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.guides.services.builtin import BuiltinContent


class Command(BaseCommand):
    help = (
        "Add built-in FAQ articles and useful materials not offered before; "
        "articles the admin edited or deleted are left alone."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        created = BuiltinContent().load()
        self.stdout.write(f"Built-in guides added: {created}.")
