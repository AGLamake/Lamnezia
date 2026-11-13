"""Fail when a gettext catalog has untranslated or fuzzy strings.

Run after `manage.py makemessages`, which adds every wrapped string that is missing from a catalog.
Needs GNU gettext (msgattrib). Usage: python scripts/check_messages.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def catalogs() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.po"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in listed.splitlines()]


def unfinished(catalog: Path) -> str:
    """Entries that still need a translator: empty or marked fuzzy by msgmerge."""
    return "".join(
        subprocess.run(
            ["msgattrib", flag, "--no-obsolete", str(catalog)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        for flag in ("--untranslated", "--only-fuzzy")
    )


def main() -> None:
    failed = False
    for catalog in catalogs():
        if entries := unfinished(catalog):
            print(f"{catalog.relative_to(ROOT)}: not translated\n{entries}")
            failed = True
    if failed:
        sys.exit("Translate the strings above, then run `make messages-check` again.")


if __name__ == "__main__":
    main()
