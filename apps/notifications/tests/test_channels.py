import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from apps.accounts.tests.registration import configure as configure_site
from apps.notifications.models import ChannelKind
from apps.notifications.services.addresses import AddressGuard
from apps.notifications.services.channels import (
    DeliveryError,
    DiscordChannel,
    DiscordDmChannel,
    HttpClient,
    Message,
    TelegramChannel,
    build_channel,
)
from apps.notifications.tests.conftest import FakeHttp
from apps.notifications.tests.factories import ChannelFactory

MESSAGE = Message(
    uid="0b8f",
    event="server_down",
    created_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    title="Server Germany is down",
    text="No answer.",
    data={"server": "Germany"},
)


class Response(io.BytesIO):
    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_telegram_sends_the_title_and_text_to_the_chat() -> None:
    http = FakeHttp()

    TelegramChannel("123:abc", "-100500", http).send(MESSAGE)  # type: ignore[arg-type]

    [post] = http.posts
    assert post.url == "https://api.telegram.org/bot123:abc/sendMessage"
    body = json.loads(post.body)
    assert body["chat_id"] == "-100500"
    assert body["text"] == "Server Germany is down\n\nNo answer."


@pytest.mark.django_db
def test_telegram_without_its_own_token_uses_the_panel_bot() -> None:
    configure_site(telegram_bot_token="999:panel")
    http = FakeHttp()
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="42", secret="")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    assert http.posts[0].url == "https://api.telegram.org/bot999:panel/sendMessage"


@pytest.mark.django_db
def test_telegram_without_any_token_fails_for_good() -> None:
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="42", secret="")

    with pytest.raises(DeliveryError) as error:
        build_channel(channel, FakeHttp()).send(MESSAGE)  # type: ignore[arg-type]

    assert not error.value.retryable


def test_discord_sends_an_embed() -> None:
    http = FakeHttp()

    DiscordChannel("https://discord.com/api/webhooks/1/x", http).send(MESSAGE)  # type: ignore[arg-type]

    embed = json.loads(http.posts[0].body)["embeds"][0]
    assert embed["title"] == "Server Germany is down"
    assert embed["description"] == "No answer."
    assert embed["timestamp"] == "2026-11-03T12:00:00+00:00"


DM_OPEN = "https://discord.com/api/v10/users/@me/channels"
DM_POST = "https://discord.com/api/v10/channels/77/messages"


def test_a_discord_direct_message_opens_the_dm_then_posts_to_it() -> None:
    http = FakeHttp(answers={DM_OPEN: b'{"id": "77"}'})

    DiscordDmChannel("bot-token", "501", http).send(MESSAGE)  # type: ignore[arg-type]

    opened, posted = http.posts
    assert (opened.url, json.loads(opened.body)) == (DM_OPEN, {"recipient_id": "501"})
    assert opened.headers["Authorization"] == "Bot bot-token"
    assert posted.url == DM_POST
    body = json.loads(posted.body)
    assert body["content"] == "**Server Germany is down**\n\nNo answer."
    assert body["allowed_mentions"] == {"parse": []}


@pytest.mark.django_db
def test_a_discord_dm_channel_uses_the_panel_bot() -> None:
    configure_site(discord_bot_token="panel-token")
    http = FakeHttp(answers={DM_OPEN: b'{"id": "77"}'})
    channel = ChannelFactory.build(kind=ChannelKind.DISCORD_DM, target="501", secret="")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    assert http.posts[1].headers["Authorization"] == "Bot panel-token"


@pytest.mark.django_db
def test_a_discord_dm_without_the_bot_token_fails_for_good() -> None:
    channel = ChannelFactory.build(kind=ChannelKind.DISCORD_DM, target="501", secret="")

    with pytest.raises(DeliveryError) as error:
        build_channel(channel, FakeHttp()).send(MESSAGE)  # type: ignore[arg-type]

    assert not error.value.retryable


def test_a_discord_dm_that_did_not_open_is_tried_again() -> None:
    http = FakeHttp(answers={DM_OPEN: b"{}"})

    with pytest.raises(DeliveryError) as error:
        DiscordDmChannel("bot-token", "501", http).send(MESSAGE)  # type: ignore[arg-type]

    assert error.value.retryable
    assert len(http.posts) == 1


def test_http_client_refuses_addresses_other_than_http() -> None:
    with pytest.raises(DeliveryError) as error:
        HttpClient(1).post("file:///etc/passwd", b"{}")

    assert not error.value.retryable


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (429, True), (404, False)])
def test_http_errors_are_retried_only_when_a_later_try_may_work(
    monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool
) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> Response:
        raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"nope"))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(DeliveryError) as error:
        HttpClient(1).post("https://api.telegram.org/botSECRET/sendMessage", b"{}")

    assert error.value.retryable is retryable
    assert str(error.value) == f"HTTP {status} nope"


def test_connection_errors_do_not_reveal_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> Response:
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(DeliveryError) as error:
        HttpClient(1).post("https://api.telegram.org/botSECRET/sendMessage", b"{}")

    assert error.value.retryable
    assert "SECRET" not in str(error.value)


def test_http_client_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[urllib.request.Request] = []

    def answer(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(request)
        return Response(b"")

    monkeypatch.setattr(urllib.request, "urlopen", answer)

    HttpClient(1).post("https://hooks.example.com/", b'{"a":1}', {"X-Test": "1"})

    [request] = sent
    assert (request.method, request.data) == ("POST", b'{"a":1}')
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-test") == "1"


def test_a_guarded_client_refuses_a_private_address_for_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", pytest.fail)

    with pytest.raises(DeliveryError) as error:
        HttpClient(1, AddressGuard(allow_private=False)).post("http://10.0.0.5/hook", b"{}")

    assert not error.value.retryable
    assert "10.0.0.5" in str(error.value)


@pytest.mark.django_db
def test_the_client_from_settings_refuses_private_addresses() -> None:
    with pytest.raises(DeliveryError) as error:
        HttpClient.from_settings().post("http://127.0.0.1:9/hook", b"{}")

    assert not error.value.retryable


def test_a_guarded_client_does_not_follow_redirects() -> None:
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    Thread(target=server.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/hook"

    with pytest.raises(DeliveryError) as error:
        HttpClient(5, AddressGuard(allow_private=True)).post(url, b"{}")

    server.server_close()
    assert str(error.value).startswith("HTTP 302")
