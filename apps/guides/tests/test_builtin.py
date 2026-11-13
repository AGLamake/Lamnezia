import json
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.guides.models import BuiltinEntry, FaqArticle, UsefulMaterial
from apps.guides.services.builtin import FIXTURES, BuiltinContent

pytestmark = pytest.mark.django_db


def _fixture_count(name: str) -> int:
    return len(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _faq_with(tmp_path: Path, slug: str) -> Path:
    """The built-in FAQ of a later version: one more article."""
    entries = json.loads((FIXTURES / "faq.json").read_text(encoding="utf-8"))
    entries.append(
        {"model": "guides.faqarticle", "fields": {"slug": slug, "title_en": "New", "body_en": "."}}
    )
    path = tmp_path / "faq.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_fresh_install_gets_every_builtin_entry() -> None:
    created = BuiltinContent().load()

    assert FaqArticle.objects.count() == _fixture_count("faq.json")
    assert UsefulMaterial.objects.count() == _fixture_count("materials.json")
    assert created == FaqArticle.objects.count() + UsefulMaterial.objects.count()


def test_loading_again_adds_nothing() -> None:
    BuiltinContent().load()

    assert BuiltinContent().load() == 0
    assert FaqArticle.objects.count() == _fixture_count("faq.json")


def test_edited_and_deleted_entries_are_left_alone() -> None:
    BuiltinContent().load()
    FaqArticle.objects.filter(slug="slow").update(title_ru="Своё")
    UsefulMaterial.objects.filter(slug="amneziawg-about-en").delete()

    BuiltinContent().load()

    assert FaqArticle.objects.get(slug="slow").title_ru == "Своё"
    assert not UsefulMaterial.objects.filter(slug="amneziawg-about-en").exists()


def test_entries_loaded_by_hand_before_are_kept_and_remembered() -> None:
    call_command("loaddata", "faq", verbosity=0)
    FaqArticle.objects.filter(slug="slow").update(title_ru="Своё")

    BuiltinContent().load()

    assert FaqArticle.objects.get(slug="slow").title_ru == "Своё"
    assert BuiltinEntry.objects.filter(model="guides.faqarticle", slug="slow").exists()


def test_update_adds_new_builtin_articles(tmp_path: Path) -> None:
    BuiltinContent().load()
    FaqArticle.objects.filter(slug="slow").delete()

    created = BuiltinContent(fixtures=(_faq_with(tmp_path, "new-in-next-version"),)).load()

    assert created == 1
    assert FaqArticle.objects.filter(slug="new-in-next-version").exists()
    assert not FaqArticle.objects.filter(slug="slow").exists()


def test_command_reports_how_many_were_added(capsys: pytest.CaptureFixture[str]) -> None:
    call_command("load_guides")

    total = _fixture_count("faq.json") + _fixture_count("materials.json")
    assert f"Built-in guides added: {total}." in capsys.readouterr().out
