"""The webhook page of the docs: its receivers accept what the panel sends."""

import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.notifications.events import EVENTS
from apps.notifications.services.channels import (
    DeliveryError,
    HttpClient,
    WebhookChannel,
    sign,
)
from apps.notifications.tests.test_channels import MESSAGE

SECRET = "s3cret"


def page(language: str) -> str:
    return (settings.BASE_DIR / "docs" / "wiki" / language / "Webhooks.md").read_text("utf-8")


def example(language: str, code: str, marker: str) -> str:
    blocks: list[str] = re.findall(rf"```{code}\n(.*?)```", page(language), re.DOTALL)
    [block] = [block for block in blocks if marker in block]
    return block


@pytest.fixture(params=["en", "ru"])
def language(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def receiver(language: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The Python receiver from the docs, listening on a free local port."""
    monkeypatch.setenv("LAMNEZIA_WEBHOOK_SECRET", SECRET)
    namespace: dict[str, Any] = {"__name__": "receiver"}
    exec(example(language, "python", "HTTPServer"), namespace)
    server = HTTPServer(("127.0.0.1", 0), namespace["Receiver"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()


def test_the_python_receiver_takes_a_signed_event(receiver: str) -> None:
    WebhookChannel(receiver, SECRET, HttpClient(5)).send(MESSAGE)


def test_the_python_receiver_turns_away_a_wrong_signature(receiver: str) -> None:
    with pytest.raises(DeliveryError) as error:
        WebhookChannel(receiver, "not the secret", HttpClient(5)).send(MESSAGE)

    assert str(error.value).startswith("HTTP 401")
    assert not error.value.retryable


# Skipped only on a developer machine without Node.js: CI installs it and must run the test.
@pytest.mark.skipif(
    shutil.which("node") is None and not os.environ.get("CI"), reason="Node.js is not installed"
)
def test_the_node_receiver_checks_the_signature(language: str, tmp_path: Path) -> None:
    script = tmp_path / "receiver.js"
    script.write_text(example(language, "javascript", "module.exports"), "utf-8")
    body = json.dumps(MESSAGE.payload())
    check = (
        f"const {{ isFromLamnezia }} = require({json.dumps(str(script))});"
        "const [secret, body, signature] = process.argv.slice(1);"
        "process.exit(isFromLamnezia(secret, Buffer.from(body), signature) ? 0 : 1);"
    )
    signature = sign(SECRET, body.encode())

    def run(secret: str) -> int:
        node = [shutil.which("node") or "node", "-e", check, secret, body, signature]
        return subprocess.run(node, check=False).returncode

    assert (run(SECRET), run("not the secret")) == (0, 1)


def test_the_schema_covers_every_event(language: str) -> None:
    schema = json.loads(example(language, "json", '"$defs"'))

    # Events only the user hears of never reach a channel.
    to_channels = {
        str(event) for event, kind in EVENTS.items() if kind.routable or not kind.to_user
    }
    assert set(schema["$defs"]) == to_channels
    assert set(schema["required"]) == set(MESSAGE.payload())


def test_the_sample_body_is_valid_json(language: str) -> None:
    sample = json.loads(example(language, "json", '"created_at": "'))

    assert set(sample) == set(MESSAGE.payload())
