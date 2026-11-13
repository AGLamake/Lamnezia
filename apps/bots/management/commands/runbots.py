"""`manage.py runbots`: the `bots` service of compose. It runs the Telegram and Discord bots as
the panel asks (Settings → Integrations → Bots) and reports their state there.
"""

import asyncio
from typing import Any

from django.core.management.base import BaseCommand

from apps.bots.adapters import discord, telegram  # noqa: F401  (registers the adapters)
from apps.bots.adapters.base import adapters
from apps.bots.services.supervisor import Supervisor


class Command(BaseCommand):
    help = "Run the messenger bots as the panel settings ask, until stopped."

    def handle(self, *args: Any, **options: Any) -> None:
        bots = adapters.all()
        self.stdout.write("Bots: " + ", ".join(a.messenger.label for a in bots))
        asyncio.run(Supervisor(bots).run())
