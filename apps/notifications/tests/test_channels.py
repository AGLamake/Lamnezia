import io
import json
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import pytest

from apps.accounts.tests.registration import configure as configure_site
from apps.core.services.outbound import DiscordApi, OutboundProxy, TelegramApi
from apps.core.tests.proxies import web_server
from apps.notifications.models import ChannelKind
from apps.notifications.services.addresses import AddressGuard
from apps.notifications.services.channels import (
    DeliveryError,
    DiscordChannel,
    DiscordDmChannel,
    HttpClient,
    Message,
    TelegramChannel,
    TelegramChat,
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

    TelegramChannel("123:abc", TelegramChat("-100500"), http).send(MESSAGE)  # type: ignore[arg-type]

    [post] = http.posts
    assert post.url == "https://api.telegram.org/bot123:abc/sendMessage"
    body = json.loads(post.body)
    assert body["chat_id"] == "-100500"
    assert body["text"] == "Server Germany is down\n\nNo answer."


@pytest.mark.parametrize(
    ("target", "chat"),
    [
        ("-100500", TelegramChat("-100500")),
        ("@news", TelegramChat("@news")),
        (" -100500:42 ", TelegramChat("-100500", 42)),
        ("@news:7", TelegramChat("@news", 7)),
    ],
)
def test_telegram_chat_address_may_name_a_forum_topic(target: str, chat: TelegramChat) -> None:
    assert TelegramChat.parse(target) == chat


@pytest.mark.parametrize("target", ["", "  ", ":42", "-100500:", "-100500:0", "-100500:x"])
def test_telegram_chat_address_refuses_a_bad_topic(target: str) -> None:
    with pytest.raises(ValueError, match="Telegram chat"):
        TelegramChat.parse(target)


@pytest.mark.django_db
def test_telegram_sends_to_the_forum_topic_of_the_address() -> None:
    http = FakeHttp()
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="-100500:42", secret="1:a")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    body = json.loads(http.posts[0].body)
    assert body["chat_id"] == "-100500"
    assert body["message_thread_id"] == 42


def test_telegram_without_a_topic_sends_to_the_chat_itself() -> None:
    http = FakeHttp()

    TelegramChannel("123:abc", TelegramChat("-100500"), http).send(MESSAGE)  # type: ignore[arg-type]

    assert "message_thread_id" not in json.loads(http.posts[0].body)


@pytest.mark.django_db
def test_telegram_with_a_broken_address_fails_without_retries() -> None:
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="-100500:x", secret="1:a")

    with pytest.raises(DeliveryError) as error:
        build_channel(channel, FakeHttp())  # type: ignore[arg-type]

    assert not error.value.retryable


@pytest.mark.django_db
def test_telegram_without_its_own_token_uses_the_panel_bot() -> None:
    configure_site(telegram_bot_token="999:panel")
    http = FakeHttp()
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="42", secret="")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    assert http.posts[0].url == "https://api.telegram.org/bot999:panel/sendMessage"


@pytest.mark.django_db
def test_telegram_calls_the_bot_api_server_of_the_settings(settings: Any) -> None:
    settings.TELEGRAM_API_SERVER = "http://bot-api:8081"
    settings.TELEGRAM_PROXY = "socks5://proxy:1080"
    configure_site(telegram_bot_token="999:panel")
    http = FakeHttp()
    channel = ChannelFactory.build(kind=ChannelKind.TELEGRAM, target="42", secret="")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    [post] = http.posts
    assert post.url == "http://bot-api:8081/bot999:panel/sendMessage"
    assert post.api == TelegramApi("http://bot-api:8081", OutboundProxy("socks5://proxy:1080"))


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
    assert http.posts[0].api is None


DISCORD_PROXY = DiscordApi(OutboundProxy("socks5://proxy:1080"))


def test_a_discord_webhook_goes_through_the_discord_proxy() -> None:
    http = FakeHttp()
    url = "https://discord.com/api/webhooks/1/x"

    DiscordChannel(url, http, DISCORD_PROXY).send(MESSAGE)  # type: ignore[arg-type]

    assert http.posts[0].api == DISCORD_PROXY


def test_a_webhook_on_another_host_keeps_the_guard_without_the_proxy() -> None:
    http = FakeHttp()
    url = "https://hooks.example.com/discord"

    DiscordChannel(url, http, DISCORD_PROXY).send(MESSAGE)  # type: ignore[arg-type]

    assert http.posts[0].api is None


@pytest.mark.django_db
def test_discord_channels_take_the_proxy_of_the_settings(settings: Any) -> None:
    settings.DISCORD_PROXY = "socks5://proxy:1080"
    configure_site(discord_bot_token="panel-token")
    http = FakeHttp(answers={DM_OPEN: b'{"id": "77"}'})
    channel = ChannelFactory.build(kind=ChannelKind.DISCORD_DM, target="501", secret="")

    build_channel(channel, http).send(MESSAGE)  # type: ignore[arg-type]

    assert [post.api for post in http.posts] == [DISCORD_PROXY, DISCORD_PROXY]


def test_the_discord_proxy_is_used_without_the_webhook_guard() -> None:
    with web_server() as (port, seen):
        api = DiscordApi(OutboundProxy(f"http://user:pw@127.0.0.1:{port}"))
        client = HttpClient(5, AddressGuard(allow_private=False))
        url = "http://discord.com/api/webhooks/1/x"

        answer = client.post(url, b"{}", api=api.route(url))

    assert json.loads(answer) == {"ok": True}
    assert seen.paths == [url]


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


def test_http_client_downloads_a_file_within_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[urllib.request.Request] = []

    def answer(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(request)
        return Response(b"5.8.0.0/16\n")

    monkeypatch.setattr(urllib.request, "urlopen", answer)

    assert HttpClient(1).get("https://lists.example.com/ru.txt", limit=11) == b"5.8.0.0/16\n"
    assert sent[0].get_method() == "GET"


def test_http_client_refuses_a_file_over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response(b"x" * 11))

    with pytest.raises(DeliveryError) as error:
        HttpClient(1).get("https://lists.example.com/ru.txt", limit=10)

    assert not error.value.retryable
    assert "lists.example.com" not in str(error.value)


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


class TestDnsRebinding:
    """The name is checked once; the request goes to that address even if the name moves."""

    PUBLIC = "8.8.8.8"

    @pytest.fixture
    def receiver(self) -> Any:
        """A private receiver that remembers the Host of every request it got."""
        hosts: list[str] = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                hosts.append(self.headers["Host"])
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Receiver)
        server.timeout = 1
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()
        yield server, hosts
        thread.join()
        server.server_close()

    @pytest.fixture
    def rebinding(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """The name answers a public address once, then the loopback; counts the lookups."""
        lookups: list[int] = []

        def resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
            lookups.append(1)
            address = self.PUBLIC if len(lookups) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 80))]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        return lookups

    def connecting(
        self, monkeypatch: pytest.MonkeyPatch, public_to: tuple[str, int] | None
    ) -> list[tuple[str, int]]:
        """Record connections; the public address goes to `public_to`, or is refused."""
        tried: list[tuple[str, int]] = []
        create_connection = socket.create_connection

        def connect(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
            tried.append(address)
            if address[0] != self.PUBLIC:
                return create_connection(address, *args, **kwargs)
            if public_to is None:
                raise ConnectionRefusedError("refused")
            return create_connection(public_to, *args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", connect)
        return tried

    def test_a_name_that_moved_after_the_check_gets_no_request(
        self, monkeypatch: pytest.MonkeyPatch, receiver: Any, rebinding: list[int]
    ) -> None:
        server, hosts = receiver
        port = server.server_port
        tried = self.connecting(monkeypatch, public_to=None)

        with pytest.raises(DeliveryError) as error:
            HttpClient(1, AddressGuard(allow_private=False)).post(
                f"http://hooks.example.com:{port}/hook", b"{}"
            )

        assert error.value.retryable
        assert tried == [(self.PUBLIC, port)]
        assert len(rebinding) == 1
        assert hosts == []

    def test_the_request_goes_to_the_checked_address_by_name(
        self, monkeypatch: pytest.MonkeyPatch, receiver: Any, rebinding: list[int]
    ) -> None:
        server, hosts = receiver
        port = server.server_port
        tried = self.connecting(monkeypatch, public_to=("127.0.0.1", port))

        HttpClient(1, AddressGuard(allow_private=False)).post(
            f"http://hooks.example.com:{port}/hook", b"{}"
        )

        assert tried == [(self.PUBLIC, port)]
        assert hosts == [f"hooks.example.com:{port}"]

    def test_a_name_that_does_not_resolve_is_tried_again_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unresolvable(*args: Any, **kwargs: Any) -> Any:
            raise socket.gaierror("no such name")

        monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
        monkeypatch.setattr(socket, "create_connection", pytest.fail)

        with pytest.raises(DeliveryError) as error:
            HttpClient(1, AddressGuard(allow_private=False)).post("https://nowhere.invalid/", b"{}")

        assert error.value.retryable


def test_the_bot_api_is_reached_without_the_webhook_guard() -> None:
    with web_server() as (port, seen):
        api = TelegramApi(f"http://127.0.0.1:{port}")
        client = HttpClient(5, AddressGuard(allow_private=False))

        answer = client.post(api.method_url("1:a", "sendMessage"), b"{}", api=api)

    assert json.loads(answer) == {"ok": True}
    assert seen.paths == ["/bot1:a/sendMessage"]


def test_a_failed_proxy_does_not_show_its_password() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    api = TelegramApi(proxy=OutboundProxy(f"socks5://user:s3cret@127.0.0.1:{port}"))

    with pytest.raises(DeliveryError) as error:
        HttpClient(2).post(api.method_url("SECRET", "sendMessage"), b"{}", api=api)

    assert error.value.retryable
    assert "s3cret" not in str(error.value)
    assert "SECRET" not in str(error.value)
