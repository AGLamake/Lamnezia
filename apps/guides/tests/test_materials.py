import datetime
import urllib.error
import urllib.request
from email.message import Message

import pytest
from django.test import Client

from apps.guides.models import MaterialTopic, UsefulMaterial
from apps.guides.services.materials import LinkChecker, MaterialLinkCheck, sections_by_topic

pytestmark = pytest.mark.django_db


def material(slug: str, topic: str = MaterialTopic.SPEED, **fields: object) -> UsefulMaterial:
    defaults: dict[str, object] = {
        "title": slug,
        "url": f"https://example.com/{slug}",
        "language": "en",
        "source": "Example",
        "topic": topic,
        "description_ru": f"Описание {slug}",
        "description_en": f"About {slug}",
    }
    return UsefulMaterial.objects.create(slug=slug, **(defaults | fields))


class FakeOpener:
    """Answers by URL and method; records what was asked."""

    def __init__(self, answers: dict[tuple[str, str], Exception | None]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> object:
        key = (request.full_url, request.get_method())
        self.calls.append(key)
        answer = self.answers.get(key)
        if answer is not None:
            raise answer
        return 200


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", Message(), None)


def test_description_falls_back_to_the_filled_language() -> None:
    item = material("only-ru", description_en="")
    assert item.description_in("en") == "Описание only-ru"
    assert item.description_in("ru") == "Описание only-ru"
    assert material("both").description_in("en-us") == "About both"


def test_sections_follow_topic_order_and_skip_empty_topics() -> None:
    speed = material("speed", MaterialTopic.SPEED)
    install = material("install", MaterialTopic.INSTALL)

    sections = sections_by_topic([speed, install], "en")

    assert [section.topic for section in sections] == [MaterialTopic.INSTALL, MaterialTopic.SPEED]
    assert sections[1].materials[0].description == "About speed"


def test_checker_falls_back_to_get_when_head_is_refused() -> None:
    url = "https://example.com/a"
    opener = FakeOpener({(url, "HEAD"): http_error(url, 405)})

    assert LinkChecker(opener=opener).check(url).ok
    assert opener.calls == [(url, "HEAD"), (url, "GET")]


def test_checker_reports_the_error() -> None:
    url = "https://example.com/gone"
    opener = FakeOpener({(url, "HEAD"): http_error(url, 404), (url, "GET"): http_error(url, 404)})
    assert LinkChecker(opener=opener).check(url).error == "HTTP 404"

    unreachable = FakeOpener(
        {(url, "HEAD"): urllib.error.URLError("no host"), (url, "GET"): TimeoutError()}
    )
    assert LinkChecker(opener=unreachable).check(url).error == "TimeoutError"


def test_link_check_marks_broken_and_clears_fixed_links() -> None:
    gone = material("gone")
    fixed = material("fixed", is_broken=True, check_error="HTTP 500")
    hidden = material("hidden", is_published=False)
    error = http_error(gone.url, 404)
    opener = FakeOpener({(gone.url, "HEAD"): error, (gone.url, "GET"): error})

    assert MaterialLinkCheck(LinkChecker(opener=opener)).run() == 1

    gone.refresh_from_db()
    fixed.refresh_from_db()
    hidden.refresh_from_db()
    assert (gone.is_broken, gone.check_error) == (True, "HTTP 404")
    assert (fixed.is_broken, fixed.check_error) == (False, "")
    assert isinstance(fixed.checked_at, datetime.date)
    assert hidden.checked_at is None


def test_page_hides_broken_and_unpublished_materials(client: Client) -> None:
    material("shown", MaterialTopic.BLOCKING, title="Shown article")
    material("broken", title="Broken article", is_broken=True)
    material("draft", title="Draft article", is_published=False)

    html = client.get("/guides/materials/", HTTP_ACCEPT_LANGUAGE="en").content.decode()

    assert "Shown article" in html
    assert "About shown" in html
    assert "https://example.com/shown" in html
    assert "Broken article" not in html
    assert "Draft article" not in html


def test_page_without_materials(client: Client) -> None:
    assert client.get("/guides/materials/").status_code == 200


def test_a_link_typed_without_a_scheme_reads_as_https() -> None:
    field = UsefulMaterial._meta.get_field("url").formfield()

    assert field.clean("example.com/awg") == "https://example.com/awg"
