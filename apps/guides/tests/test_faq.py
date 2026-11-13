import pytest
from django.core.exceptions import ValidationError

from apps.guides.models import FaqArticle
from apps.guides.services.faq import LocalizedArticle
from apps.guides.services.markdown import render_markdown


def article(**fields: str) -> FaqArticle:
    return FaqArticle(slug="no-connection", **fields)


def test_shows_the_asked_language() -> None:
    faq = article(title_ru="Нет связи", body_ru="Текст", title_en="No connection", body_en="Text")

    assert faq.title_in("en") == "No connection"
    assert faq.body_in("en-us") == "Text"
    assert faq.title_in("ru") == "Нет связи"


def test_missing_language_falls_back_to_the_other() -> None:
    assert article(title_ru="Нет связи", body_ru="Текст").title_in("en") == "Нет связи"
    assert article(title_en="No connection", body_en="Text").body_in("ru") == "Text"


def test_needs_title_and_text_in_one_language() -> None:
    with pytest.raises(ValidationError):
        article(title_ru="Нет связи", body_en="Text").clean()

    article(title_en="No connection", body_en="Text").clean()


def test_markdown_is_rendered_and_cleaned() -> None:
    html = render_markdown("## Steps\n\n1. Open **Amnezia**\n\n<script>alert(1)</script>")

    assert "<h2>Steps</h2>" in html
    assert "<strong>Amnezia</strong>" in html
    assert "<script" not in html


def test_markdown_links_are_kept_without_javascript() -> None:
    html = render_markdown("[app](https://amnezia.org) [bad](javascript:alert(1))")

    assert 'href="https://amnezia.org"' in html
    assert "javascript:" not in html


def test_localized_article_renders_body() -> None:
    localized = LocalizedArticle(article(title_en="No connection", body_en="*Check* the key"), "en")

    assert localized.title == "No connection"
    assert localized.body_html == "<p><em>Check</em> the key</p>"
