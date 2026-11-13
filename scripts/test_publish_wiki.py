import re
from pathlib import Path

import pytest
from publish_wiki import ROOT, Wiki, WikiError, anchor, publish

BLOB = "https://github.com/owner/repo/blob/main"


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path, "docs/wiki/_Sidebar.md", "[Home](en/Home.md) · [Главная](ru/Home.md)\n")
    write(tmp_path, "docs/wiki/en/Home.md", "# Home\n\n## Keys and plans\n")
    write(tmp_path, "docs/wiki/ru/Home.md", "# Главная\n\n## Ключи и тарифы\n")
    write(tmp_path, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n")
    return tmp_path


def render(repo: Path, page: str, text: str) -> str:
    write(repo, f"docs/wiki/{page}", text)
    return Wiki(repo, BLOB).render(repo / "docs" / "wiki" / page)


def test_anchor_follows_github() -> None:
    assert anchor("5. Manual install") == "5-manual-install"
    assert anchor("Контейнер: перезапуск, остановка") == "контейнер-перезапуск-остановка"


def test_russian_pages_get_a_prefix(repo: Path) -> None:
    assert sorted(Wiki(repo, BLOB).pages()) == ["Home.md", "_Sidebar.md", "ru-Home.md"]


def test_links_become_wiki_and_github_links(repo: Path) -> None:
    text = render(
        repo,
        "ru/Keys.md",
        "[a](Home.md#ключи-и-тарифы) [b](../en/Home.md) [c](../../../CHANGELOG.md#unreleased) "
        "[d](https://example.com/x.md) [e](#ключи)\n\n## Ключи\n",
    )
    assert text.startswith(
        f"[a](ru-Home#ключи-и-тарифы) [b](Home) [c]({BLOB}/CHANGELOG.md#unreleased) "
        "[d](https://example.com/x.md) [e](#ключи)"
    )


def test_sidebar_links_to_both_languages(repo: Path) -> None:
    sidebar = Wiki(repo, BLOB).pages()["_Sidebar.md"]
    assert sidebar == "[Home](Home) · [Главная](ru-Home)\n"


def test_pictures_link_to_the_file_itself(repo: Path) -> None:
    write(repo, "docs/images/shot.png", "png")
    text = render(repo, "en/Shots.md", "[![Shot](../../images/shot.png)](../../images/shot.png)\n")
    picture, page = f"{BLOB}/docs/images/shot.png?raw=true", f"{BLOB}/docs/images/shot.png"
    assert text == f"[![Shot]({picture})]({page})\n"


def test_links_in_code_blocks_stay(repo: Path) -> None:
    source = "```md\n[x](Missing.md)\n```\n"
    assert render(repo, "en/Code.md", source) == source


@pytest.mark.parametrize("link", ["Missing.md", "Home.md#no-such-heading", "#nowhere"])
def test_broken_link_fails(repo: Path, link: str) -> None:
    with pytest.raises(WikiError, match=re.escape(link)):
        render(repo, "en/Broken.md", f"[x]({link})\n")


def test_include_inserts_the_file_without_its_title(repo: Path) -> None:
    write(repo, "docs/NOTES.md", "# Notes\n\nSee [changes](../CHANGELOG.md).\n")
    text = render(repo, "en/Notes.md", "# Notes\n\n<!-- include: docs/NOTES.md -->\n")
    assert text == f"# Notes\n\nSee [changes]({BLOB}/CHANGELOG.md).\n"


def test_missing_include_fails(repo: Path) -> None:
    with pytest.raises(WikiError, match=re.escape("docs/GONE.md")):
        render(repo, "en/Notes.md", "<!-- include: docs/GONE.md -->\n")


def test_publish_replaces_old_pages(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "wiki"
    out.mkdir()
    (out / "Old.md").write_text("old", "utf-8")
    (out / "logo.png").write_bytes(b"png")

    assert publish(Wiki(repo, BLOB), out) == len(Wiki(repo, BLOB).pages())
    assert sorted(path.name for path in out.iterdir()) == [
        "Home.md",
        "_Sidebar.md",
        "logo.png",
        "ru-Home.md",
    ]


def test_repository_wiki_has_no_broken_links() -> None:
    assert Wiki(ROOT, BLOB).pages()


def test_both_languages_have_the_same_pages() -> None:
    names = {
        lang: {path.name for path in (ROOT / "docs/wiki" / lang).glob("*.md")}
        for lang in ("en", "ru")
    }
    assert names["en"] == names["ru"]


@pytest.mark.parametrize("language", ["en", "ru"])
def test_configuration_lists_every_variable(language: str) -> None:
    variables = set(
        re.findall(r"\bLAMNEZIA_[A-Z0-9_]+", (ROOT / ".env.example").read_text("utf-8"))
    )
    page = (ROOT / "docs/wiki" / language / "Configuration.md").read_text("utf-8")
    assert {name for name in variables if name not in page} == set()
