"""Turn docs/wiki into the pages of the GitHub Wiki.

The GitHub Wiki is one flat folder, so English pages keep their names and Russian pages get the
`ru-` prefix. Relative links become wiki links (pages) or GitHub links (other repository files;
a picture links to the raw file); a link to a missing file or heading fails.
`<!-- include: path -->` lines are replaced with the file at that repository path, for pages kept
outside the wiki (architecture, code style).

Usage: python scripts/publish_wiki.py --out <wiki checkout> [--repo owner/name] [--ref main]
"""

import argparse
import os
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIXES = {"en": "", "ru": "ru-"}
SIDEBAR = "_Sidebar.md"

# [text](href) or ![alt](src); the text may hold one picture: [![alt](src)](href).
LINK = re.compile(r"(!?)\[((?:[^\[\]]|!\[[^\[\]]*\]\([^)\s]+\))*)\]\(([^)\s]+)\)")
INCLUDE = re.compile(r"^<!-- include: (\S+) -->$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
EXTERNAL = re.compile(r"^([a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


class WikiError(Exception):
    """A page cannot be published as written; the message names the file and the link."""


def anchor(heading: str) -> str:
    """Anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def outside_code(text: str) -> list[tuple[str, bool]]:
    """Split text into (part, is_code) so links in fenced code blocks stay as they are."""
    parts: list[tuple[str, bool]] = []
    position = 0
    for block in FENCE.finditer(text):
        parts += [(text[position : block.start()], False), (block.group(), True)]
        position = block.end()
    return [*parts, (text[position:], False)]


@cache
def anchors(path: Path) -> frozenset[str]:
    prose = "".join(part for part, is_code in outside_code(path.read_text("utf-8")) if not is_code)
    return frozenset(anchor(heading) for heading in HEADING.findall(prose))


@dataclass(frozen=True)
class Wiki:
    """The wiki sources of a repository and where its other files are browsed on GitHub."""

    root: Path
    blob_url: str

    @property
    def source(self) -> Path:
        return self.root / "docs" / "wiki"

    def pages(self) -> dict[str, str]:
        """Wiki file name → rendered Markdown."""
        pages = {SIDEBAR: self.render(self.source / SIDEBAR)}
        for language, prefix in PREFIXES.items():
            for path in sorted((self.source / language).glob("*.md")):
                pages[prefix + path.name] = self.render(path)
        return pages

    def render(self, path: Path) -> str:
        text = INCLUDE.sub(
            lambda match: self._include(match.group(1), path), path.read_text("utf-8")
        )
        return self._links(text, path)

    def page_name(self, path: Path) -> str | None:
        """Wiki name of a source page, or None when the path is not a wiki page."""
        language = path.parent.name
        if path.suffix != ".md" or path.parent.parent != self.source or language not in PREFIXES:
            return None
        return PREFIXES[language] + path.stem

    def _include(self, name: str, page: Path) -> str:
        path = self.root / name
        if not path.is_file():
            raise WikiError(f"{self._relative(page)}: included file {name} does not exist")
        text = path.read_text("utf-8")
        if text.startswith("# "):  # the page has its own title
            text = text.split("\n", 1)[1]
        return self._links(text.strip("\n"), path)

    def _links(self, text: str, path: Path) -> str:
        def replace(match: re.Match[str]) -> str:
            picture, text, href = match.groups()
            target = self._target(href, path)
            if picture and target.startswith(self.blob_url):
                target += "?raw=true"  # a picture needs the file itself, not its GitHub page
            return f"{picture}[{LINK.sub(replace, text)}]({target})"

        return "".join(
            part if is_code else LINK.sub(replace, part) for part, is_code in outside_code(text)
        )

    def _target(self, href: str, path: Path) -> str:
        if EXTERNAL.match(href):
            return href
        address, _, fragment = href.partition("#")
        target = (path.parent / address).resolve() if address else path
        if not target.exists():
            raise WikiError(f"{self._relative(path)}: link {href} leads to a missing file")
        if fragment and target.suffix == ".md" and fragment not in anchors(target):
            raise WikiError(f"{self._relative(path)}: link {href} leads to a missing heading")
        suffix = f"#{fragment}" if fragment else ""
        if not address:
            return suffix
        if name := self.page_name(target):
            return name + suffix
        return f"{self.blob_url}/{self._relative(target)}{suffix}"

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()


def publish(wiki: Wiki, out: Path) -> int:
    """Replace the Markdown pages in `out` (a wiki checkout) with the rendered ones."""
    pages = wiki.pages()
    for stale in out.glob("*.md"):
        stale.unlink()
    for name, text in pages.items():
        (out / name).write_text(text, "utf-8", newline="\n")
    return len(pages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True, help="checkout of the wiki repository")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "AGLamake/Lamnezia"))
    parser.add_argument("--ref", default="releases", help="branch the repository links point to")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    wiki = Wiki(ROOT, f"https://github.com/{args.repo}/blob/{args.ref}")
    try:
        count = publish(wiki, args.out)
    except WikiError as error:
        raise SystemExit(f"Wiki not published: {error}") from error
    print(f"{count} wiki pages written to {args.out}")


if __name__ == "__main__":
    main()
