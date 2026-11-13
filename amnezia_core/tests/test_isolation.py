import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent
FORBIDDEN = ("django", "apps", "paramiko", "socket", "requests")


@pytest.mark.parametrize("path", sorted(PACKAGE.rglob("*.py")), ids=lambda path: path.name)
def test_package_stays_pure_python(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    imported += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]

    assert not [name for name in imported if name.split(".")[0] in FORBIDDEN]
