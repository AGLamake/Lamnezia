#!/usr/bin/env python
import os
import sys

from django.core.management import execute_from_command_line

# Commands that load the settings but never use the keys: they run in a fresh clone without .env.
KEYLESS_COMMANDS = {"makemessages", "compilemessages"}


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if KEYLESS_COMMANDS.intersection(sys.argv[1:2]):
        os.environ.setdefault("LAMNEZIA_SECRET_KEY", "keyless-command-only")
        os.environ.setdefault("LAMNEZIA_ENCRYPTION_KEY", "keyless-command-only")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
