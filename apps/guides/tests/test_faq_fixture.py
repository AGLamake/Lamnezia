import re

import pytest
from django.core.management import call_command
from django.urls import Resolver404, resolve

from apps.guides.models import FaqArticle

# Relative links in article Markdown: [text](/guides/...).
_LINK = re.compile(r"\]\((/[^)\s]*)\)")


@pytest.fixture
def articles(db: None) -> list[FaqArticle]:
    call_command("loaddata", "faq", verbosity=0)
    return list(FaqArticle.objects.all())


def test_has_at_least_ten_articles_in_both_languages(articles: list[FaqArticle]) -> None:
    assert len(articles) >= 10
    for article in articles:
        assert article.title_ru and article.body_ru, article.slug
        assert article.title_en and article.body_en, article.slug
        article.full_clean()


def test_reloading_updates_articles_by_slug(articles: list[FaqArticle]) -> None:
    edited = FaqArticle.objects.get(slug="slow")
    edited.title_ru = "Изменено"
    edited.save()
    FaqArticle.objects.create(slug="own", title_ru="Своя статья", body_ru="Текст")

    call_command("loaddata", "faq", verbosity=0)

    assert FaqArticle.objects.count() == len(articles) + 1
    assert FaqArticle.objects.get(slug="slow").title_ru == "Медленно работает"
    assert FaqArticle.objects.filter(slug="own").exists()


def test_links_lead_to_existing_pages(articles: list[FaqArticle]) -> None:
    slugs = {article.slug for article in articles}
    for article in articles:
        for url in _LINK.findall(article.body_ru + article.body_en):
            try:
                match = resolve(url)
            except Resolver404:
                pytest.fail(f"{article.slug}: {url} does not exist")
            if match.url_name == "faq-article":
                assert match.kwargs["slug"] in slugs, f"{article.slug}: {url}"
