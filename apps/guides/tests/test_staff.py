"""FAQ articles and useful materials in the panel: the administrator's pages and the audit."""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.guides.models import BuiltinEntry, FaqArticle, UsefulMaterial
from apps.guides.services.builtin import BuiltinContent

pytestmark = pytest.mark.django_db

ARTICLE: dict[str, Any] = {
    "slug": "tv",
    "title_ru": "Ключ на телевизоре",
    "body_ru": "Установите **Amnezia**.",
    "title_en": "",
    "body_en": "",
    "credit": "",
    "is_published": "on",
}
MATERIAL: dict[str, Any] = {
    "slug": "awg-speed",
    "title": "Speeding up AmneziaWG",
    "url": "https://example.com/awg-speed",
    "language": "en",
    "source": "example.com",
    "topic": "speed",
    "description_ru": "",
    "description_en": "How to tune MTU.",
    "credit": "",
    "is_published": "on",
}


@pytest.fixture
def admin(client: Client) -> User:
    member = UserFactory(role=Role.ADMIN)
    client.force_login(member)
    return member


def article(slug: str, position: int = 0, **fields: Any) -> FaqArticle:
    return FaqArticle.objects.create(
        slug=slug, title_ru=slug, body_ru="text", position=position, **fields
    )


def material(slug: str, position: int = 0) -> UsefulMaterial:
    return UsefulMaterial.objects.create(
        slug=slug,
        title=slug,
        url=f"https://example.com/{slug}",
        language="en",
        source="example.com",
        topic="speed",
        description_en="text",
        position=position,
    )


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("pk").values_list("action", flat=True))


def test_the_lists_show_articles_and_materials(client: Client, admin: User) -> None:
    article("hidden-one", is_published=False)
    material("tuning")

    faq = client.get(reverse("guides:staff", args=["faq"]))
    materials = client.get(reverse("guides:staff", args=["materials"]))

    assert faq.status_code == 200 and b"hidden-one" in faq.content
    assert materials.status_code == 200 and b"tuning" in materials.content


def test_an_unknown_shelf_is_missing(client: Client, admin: User) -> None:
    assert client.get(reverse("guides:staff", args=["other"])).status_code == 404


def test_an_article_is_created_last_and_edited(client: Client, admin: User) -> None:
    article("first", position=10)

    created = client.post(reverse("guides:staff-add", args=["faq"]), ARTICLE)
    entry = FaqArticle.objects.get(slug="tv")
    edited = client.post(
        reverse("guides:staff-edit", args=["faq", entry.pk]),
        {**ARTICLE, "title_ru": "Телевизор и ключ"},
    )

    assert created.status_code == edited.status_code == 302
    entry.refresh_from_db()
    assert entry.title_ru == "Телевизор и ключ" and entry.position > 10
    assert actions() == ["guides.article_create", "guides.article_update"]


def test_a_material_is_created_and_an_invalid_one_is_not(client: Client, admin: User) -> None:
    url = reverse("guides:staff-add", args=["materials"])

    invalid = client.post(url, {**MATERIAL, "description_en": ""})
    created = client.post(url, MATERIAL)

    assert invalid.status_code == 200 and not invalid.context["form"].is_valid()
    assert created.status_code == 302
    assert UsefulMaterial.objects.get().slug == "awg-speed"
    assert actions() == ["guides.material_create"]


def test_publishing_hides_and_shows_an_entry(client: Client, admin: User) -> None:
    entry = material("tuning")
    url = reverse("guides:staff-publish", args=["materials", entry.pk])

    client.post(url, {"published": "0"})
    hidden = UsefulMaterial.objects.get().is_published
    client.post(url, {"published": "1"})

    assert hidden is False and UsefulMaterial.objects.get().is_published is True
    assert actions() == ["guides.material_hide", "guides.material_publish"]


def test_moving_an_entry_swaps_it_with_its_neighbour(client: Client, admin: User) -> None:
    first, _second, third = article("a", 10), article("b", 10), article("c", 30)

    client.post(reverse("guides:staff-move", args=["faq", third.pk]), {"direction": "up"})
    client.post(reverse("guides:staff-move", args=["faq", first.pk]), {"direction": "up"})

    assert list(FaqArticle.objects.values_list("slug", flat=True)) == ["a", "c", "b"]
    assert actions() == ["guides.article_move"]  # the first one has nowhere to go


def test_deleting_an_entry(client: Client, admin: User) -> None:
    entry = article("old")

    response = client.post(reverse("guides:staff-delete", args=["faq", entry.pk]))

    assert response.status_code == 302 and not FaqArticle.objects.exists()
    assert actions() == ["guides.article_delete"]


def test_a_deleted_builtin_article_is_listed_and_restored(client: Client, admin: User) -> None:
    BuiltinContent().load()
    gone = FaqArticle.objects.order_by("pk").first()
    assert gone is not None
    slug = gone.slug
    client.post(reverse("guides:staff-delete", args=["faq", gone.pk]))
    offered = BuiltinEntry.objects.deleted().get()

    listed = client.get(reverse("guides:staff-deleted"))
    restored = client.post(reverse("guides:staff-restore", args=[offered.pk]))

    assert slug.encode() in listed.content
    assert restored.status_code == 302 and FaqArticle.objects.filter(slug=slug).exists()
    assert not BuiltinEntry.objects.deleted().exists()
    assert actions()[-1] == "guides.builtin_restore"


def test_the_preview_is_safe_and_saves_nothing(client: Client, admin: User) -> None:
    url = reverse("guides:staff-preview")

    response = client.post(url, {"text": "**Bold** <script>alert(1)</script>"})

    html = response.content.decode()
    assert "<strong>Bold</strong>" in html and "<script>" not in html
    assert client.get(url).status_code == 405
    assert not FaqArticle.objects.exists() and not AuditEvent.objects.exists()


def test_the_form_offers_the_preview(client: Client, admin: User) -> None:
    page = client.get(reverse("guides:staff-add", args=["faq"])).content.decode()

    preview = reverse("guides:staff-preview")
    assert page.count(f'data-markdown-preview="{preview}"') == 2
    assert "js/markdown-preview.js" in page


@pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
def test_only_an_administrator_edits_articles(client: Client, role: str) -> None:
    entry = article("kept")
    client.force_login(UserFactory(role=role))

    pages = [
        client.get(reverse("guides:staff", args=["faq"])),
        client.get(reverse("guides:staff-deleted")),
        client.post(reverse("guides:staff-delete", args=["faq", entry.pk])),
        client.post(reverse("guides:staff-preview"), {"text": "a"}),
    ]

    assert {page.status_code for page in pages} == {403}
    assert FaqArticle.objects.filter(pk=entry.pk).exists()


def test_the_menu_links_the_articles(client: Client, admin: User) -> None:
    page = client.get(reverse("guides:staff", args=["faq"]))

    assert reverse("guides:staff", args=["faq"]).encode() in page.content
