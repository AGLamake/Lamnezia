"""Requests to addresses users give go through `HttpClient`: a new direct HTTP client fails here.

`HttpClient` (apps/notifications/services/channels.py) with its `AddressGuard` refuses private
networks and connects only to the addresses it checked (pinned.py). A module that opens HTTP
by itself skips both, so only the modules below may import an HTTP client (CODE_STYLE.md 2).
"""

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("apps", "config", "amnezia_core")
CLIENTS = ("httpx", "requests", "aiohttp", "urllib3", "pycurl", "urllib.request", "http.client")

# Each one reaches only fixed hosts, or is the guarded client itself.
ALLOWED = {
    # The guarded client and its pinned connections.
    "apps/notifications/services/channels.py",
    "apps/notifications/services/pinned.py",
    # Telegram, Discord and GitHub through the configured proxy.
    "apps/core/services/outbound.py",
    # Telegram and Discord APIs for linking accounts.
    "apps/accounts/services/messengers.py",
    # The S3 storage of archive copies the administrator set.
    "apps/backups/services/archive_remote.py",
    # The Telegram bot: the aiohttp server of its webhook.
    "apps/bots/adapters/telegram.py",
    # GitHub releases and the weekly check of the staff's material links.
    "apps/guides/services/materials.py",
    "apps/guides/services/releases.py",
}


def imported_modules(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def uses_client(module: str) -> bool:
    return any(module == client or module.startswith(f"{client}.") for client in CLIENTS)


def panel_modules() -> Iterator[Path]:
    for package in PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            if not {"tests", "migrations"} & set(path.relative_to(ROOT).parts):
                yield path


def direct_clients() -> set[str]:
    found = set()
    for path in panel_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(uses_client(module) for module in imported_modules(tree)):
            found.add(path.relative_to(ROOT).as_posix())
    return found


def test_http_clients_are_imported_only_where_allowed() -> None:
    unexpected = direct_clients() - ALLOWED

    assert not unexpected, (
        f"Direct HTTP client in {sorted(unexpected)}: send requests to user-given addresses "
        "with HttpClient.from_settings(), or add a fixed-host module to ALLOWED with a reason."
    )


def test_every_allowed_module_still_needs_it() -> None:
    assert not ALLOWED - direct_clients()


def test_the_check_sees_every_kind_of_import() -> None:
    sources = (
        "import httpx",
        "import urllib.request",
        "from urllib import request",
        "from http.client import HTTPSConnection",
        "from aiohttp import ClientSession",
    )
    for source in sources:
        assert any(uses_client(module) for module in imported_modules(ast.parse(source))), source
    assert not any(uses_client(m) for m in imported_modules(ast.parse("from urllib import parse")))
