import pytest
from django.core.management import call_command

from apps.guides.models import MaterialLanguage, MaterialTopic, UsefulMaterial


@pytest.fixture
def materials(db: None) -> list[UsefulMaterial]:
    call_command("loaddata", "materials", verbosity=0)
    return list(UsefulMaterial.objects.all())


def test_has_at_least_ten_checked_materials(materials: list[UsefulMaterial]) -> None:
    assert len(materials) >= 10
    for material in materials:
        assert material.description_ru and material.description_en, material.slug
        assert material.url.startswith("https://"), material.slug
        assert material.checked_at is not None and not material.is_broken, material.slug
        material.full_clean()


def test_covers_every_topic_in_both_languages(materials: list[UsefulMaterial]) -> None:
    assert {material.topic for material in materials} == set(MaterialTopic.values)
    assert {material.language for material in materials} == set(MaterialLanguage.values)


def test_links_are_unique(materials: list[UsefulMaterial]) -> None:
    urls = [material.url for material in materials]
    assert len(urls) == len(set(urls))


def test_reloading_updates_materials_by_slug(materials: list[UsefulMaterial]) -> None:
    edited = UsefulMaterial.objects.get(slug="amneziawg-about-en")
    edited.title = "Edited"
    edited.save()

    call_command("loaddata", "materials", verbosity=0)

    assert UsefulMaterial.objects.count() == len(materials)
    assert UsefulMaterial.objects.get(slug="amneziawg-about-en").title == "AmneziaWG"
