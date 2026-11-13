"""Downloading the Tailwind CLI survives a temporary failure of GitHub."""

import io
import urllib.error
from collections.abc import Iterator
from email.message import Message

import pytest
import tailwind


class Answers:
    """Stands in for urlopen: raises the queued errors, then returns the body."""

    def __init__(self, *errors: Exception) -> None:
        self.errors: Iterator[Exception] = iter(errors)
        self.calls = 0

    def __call__(self, url: str, timeout: float) -> io.BytesIO:
        self.calls += 1
        error = next(self.errors, None)
        if error is not None:
            raise error
        return io.BytesIO(b"binary")


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://github.com", code, "error", Message(), None)


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def test_a_server_error_and_a_dropped_connection_are_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = [http_error(504), urllib.error.URLError("reset"), TimeoutError()]
    answers = Answers(*errors)
    monkeypatch.setattr("urllib.request.urlopen", answers)

    assert tailwind._download("https://github.com/cli") == b"binary"
    assert answers.calls == len(errors) + 1


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = Answers(http_error(404))
    monkeypatch.setattr("urllib.request.urlopen", answers)

    with pytest.raises(urllib.error.HTTPError):
        tailwind._download("https://github.com/cli")
    assert answers.calls == 1


def test_it_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = Answers(*[http_error(503)] * tailwind.ATTEMPTS)
    monkeypatch.setattr("urllib.request.urlopen", answers)

    with pytest.raises(urllib.error.HTTPError):
        tailwind._download("https://github.com/cli")
    assert answers.calls == tailwind.ATTEMPTS
