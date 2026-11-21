"""The app versions for AmneziaWG 3.1 in the code match the table of both wiki pages «Protocols»."""

import re
from pathlib import Path

import pytest
from django.utils import translation

from apps.portal.services.app_versions import AWG3_APPS

WIKI = Path(__file__).resolve().parents[3] / "docs" / "wiki"
TABLE_HEAD = re.compile(r"^\| (App|Приложение) \| Android \|")
VERSION = re.compile(r"^\d+(\.\d+)+$")

Versions = dict[tuple[str, str], str]


def split(platforms: str) -> list[str]:
    return [platform.strip() for platform in platforms.split(",")]


def code_versions() -> Versions:
    with translation.override("en"):
        return {
            (app.app, platform): app.version
            for app in AWG3_APPS
            for platform in split(str(app.platforms))
        }


def wiki_versions(page: Path) -> Versions:
    """Cells holding a plain version; a cell like «Linux: amneziawg-tools 3.1» is not one."""
    lines = iter(page.read_text(encoding="utf-8").splitlines())
    head = next(line for line in lines if TABLE_HEAD.match(line))
    columns = [cell.strip() for cell in head.strip("|").split("|")][1:]
    next(lines)
    versions: Versions = {}
    for row in lines:
        if not row.startswith("|"):
            break
        app, *cells = (cell.strip() for cell in row.strip("|").split("|"))
        for platforms, cell in zip(columns, cells, strict=True):
            if VERSION.match(cell):
                versions.update({(app, platform): cell for platform in split(platforms)})
    return versions


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_wiki_table_matches_the_code(lang: str) -> None:
    assert wiki_versions(WIKI / lang / "Protocols.md") == code_versions()
