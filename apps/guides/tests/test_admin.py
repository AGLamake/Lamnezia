import pytest
from django.test import Client

from apps.accounts.models import Role, User
from apps.guides.models import BuiltinEntry, FaqArticle, UsefulMaterial
from apps.guides.services.builtin import BuiltinContent

pytestmark = pytest.mark.django_db

PREVIEW = "/admin/guides/faqarticle/preview/"
JOURNAL = "/admin/guides/builtinentry/"


def test_the_preview_renders_markdown_as_the_public_page(admin_client: Client) -> None:
    response = admin_client.post(PREVIEW, {"text": "**Bold** <script>alert(1)</script>"})

    assert response.status_code == 200
    html = response.content.decode()
    assert "<strong>Bold</strong>" in html
    assert "<script>" not in html


def test_the_preview_only_answers_posts(admin_client: Client) -> None:
    assert admin_client.get(PREVIEW).status_code == 405


def test_the_preview_is_for_administrators_only(client: Client) -> None:
    client.force_login(User.objects.create_user("moderator", password="x", role=Role.MODERATOR))

    response = client.post(PREVIEW, {"text": "a"})

    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")


def test_the_article_form_offers_the_preview(admin_client: Client) -> None:
    page = admin_client.get("/admin/guides/faqarticle/add/").content.decode()

    assert page.count(f'data-markdown-preview="{PREVIEW}"') == 2
    assert "js/markdown-preview.js" in page


def test_the_journal_lists_only_deleted_builtin_entries(admin_client: Client) -> None:
    BuiltinContent().load()
    FaqArticle.objects.filter(slug="slow").delete()

    page = admin_client.get(JOURNAL).content.decode()

    assert ">slow<" in page
    assert list(BuiltinEntry.objects.deleted().values_list("slug", flat=True)) == ["slow"]


def test_restoring_brings_a_deleted_entry_back(admin_client: Client) -> None:
    BuiltinContent().load()
    FaqArticle.objects.filter(slug="slow").delete()
    UsefulMaterial.objects.filter(slug="amneziawg-about-en").delete()
    selected = BuiltinEntry.objects.deleted().values_list("pk", flat=True)

    response = admin_client.post(
        JOURNAL, {"action": "restore", "_selected_action": list(selected)}, follow=True
    )

    assert "2 entries are restored." in response.content.decode()
    assert FaqArticle.objects.filter(slug="slow").exists()
    assert UsefulMaterial.objects.filter(slug="amneziawg-about-en").exists()
    assert not BuiltinEntry.objects.deleted().exists()


def test_an_entry_the_panel_has_again_or_no_longer_ships_is_skipped() -> None:
    BuiltinContent().load()
    FaqArticle.objects.filter(slug="slow").delete()
    FaqArticle.objects.create(slug="slow", title_en="Own", body_en=".")
    gone = BuiltinEntry.objects.create(model="guides.faqarticle", slug="dropped-later")

    assert BuiltinContent().restore([*BuiltinEntry.objects.filter(slug="slow"), gone]) == 0
    assert FaqArticle.objects.get(slug="slow").title_en == "Own"
