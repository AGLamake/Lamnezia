"""`makemessages` that writes the same catalog on every run and every gettext version.

Entries are sorted by msgid instead of the order the sources were scanned in, lines are not
wrapped, and the creation date is left out of the header, so a run without new strings changes
nothing and a run with new strings only adds them.
"""

import re
from pathlib import Path
from typing import Any

from django.core.management.commands import makemessages

_CREATION_DATE = re.compile(r'^"POT-Creation-Date: [^"]*"\n', re.MULTILINE)


class Command(makemessages.Command):
    def handle(self, *args: Any, **options: Any) -> None:
        options["no_wrap"] = True
        # msguniq sorts the template and msgattrib the catalog; xgettext and msgmerge keep that
        # order (their own --sort-output is deprecated in newer gettext).
        self.msguniq_options = [*self.msguniq_options, "--sort-output"]
        self.msgattrib_options = [*self.msgattrib_options, "--sort-output"]
        super().handle(*args, **options)

    def write_po_file(self, potfile: str, locale: str) -> None:
        super().write_po_file(potfile, locale)
        pofile = Path(potfile).parent / locale / "LC_MESSAGES" / f"{self.domain}.po"
        text = pofile.read_text(encoding="utf-8")
        pofile.write_text(_CREATION_DATE.sub("", text, count=1), encoding="utf-8", newline="\n")
