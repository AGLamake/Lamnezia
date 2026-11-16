"""Backups stand below the server groups: restoring a copy needs no knowledge of a group."""

import ast
from pathlib import Path

BACKUPS = Path(__file__).resolve().parents[1]


def _imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}
    return names


def test_backups_do_not_import_the_groups() -> None:
    importing = [
        path.relative_to(BACKUPS).as_posix()
        for path in BACKUPS.rglob("*.py")
        if any(name.startswith("apps.groups") for name in _imported(path))
    ]

    assert importing == []
