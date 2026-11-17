"""`manage.py relocation status|arrive MOVE_ID`: the moving panel asks its copy over SSH."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.relocation.errors import MoveError
from apps.relocation.models import PanelMove
from apps.relocation.services.completion import arrive_here

MISSING = "missing"


class Command(BaseCommand):
    help = (
        "State of a panel move in this copy (status), or let a moved copy accept changes (arrive)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=["status", "arrive"])
        parser.add_argument("move_id", type=int)

    def handle(self, *args: Any, **options: Any) -> None:
        move_id = options["move_id"]
        if options["action"] == "status":
            move = PanelMove.objects.filter(pk=move_id).first()
            self.stdout.write(move.state if move else MISSING)
            return
        try:
            self.stdout.write(arrive_here(move_id).state)
        except (PanelMove.DoesNotExist, MoveError) as exc:
            raise CommandError(str(exc)) from exc
