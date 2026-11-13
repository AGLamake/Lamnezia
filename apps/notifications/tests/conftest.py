import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from django.db import transaction

from apps.notifications.services.channels import DeliveryError, HttpClient


@dataclass
class Post:
    url: str
    body: bytes
    headers: dict[str, str]


@dataclass
class FakeHttp:
    """Records POSTs instead of sending them; URLs in `failing` answer with the given error."""

    posts: list[Post] = field(default_factory=list)
    failing: dict[str, DeliveryError] = field(default_factory=dict)

    answers: dict[str, bytes] = field(default_factory=dict)

    def post(self, url: str, body: bytes, headers: dict[str, str] | None = None) -> bytes:
        if url in self.failing:
            raise self.failing[url]
        self.posts.append(Post(url, body, headers or {}))
        return self.answers.get(url, b"")

    def to(self, url: str) -> list[Post]:
        return [post for post in self.posts if post.url == url]


# A documentation address that counts as public; tests never reach the network.
PUBLIC_ADDRESS = "93.184.215.14"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names under example.com resolve to a public address without asking DNS."""
    real = socket.getaddrinfo

    def resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host).endswith("example.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHttp]:
    fake = FakeHttp()
    monkeypatch.setattr(
        HttpClient, "post", lambda self, *args, **kwargs: fake.post(*args, **kwargs)
    )
    yield fake


@pytest.fixture
def commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs wait for the commit, which a test never makes: queue them at once."""
    monkeypatch.setattr(transaction, "on_commit", lambda queue: queue())
