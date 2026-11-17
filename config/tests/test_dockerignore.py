"""The image gets the files git tracks and no local file of a working copy.

The rules of .dockerignore are applied here as Docker applies them: the last pattern that matches
a path or one of its folders decides, `!` keeps.
"""

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from django.conf import settings

# Tracked, but of no use in the image.
LEFT_OUT = frozenset({".github", "docs"})


class DockerIgnore:
    def __init__(self, text: str) -> None:
        self._rules = [
            (line.startswith("!"), self._regex(line.removeprefix("!")))
            for line in map(str.strip, text.splitlines())
            if line and not line.startswith("#")
        ]

    def keeps(self, path: str) -> bool:
        candidates = [path, *(str(parent) for parent in PurePosixPath(path).parents)][:-1]
        kept = True
        for keep, regex in self._rules:
            if any(regex.fullmatch(candidate) for candidate in candidates):
                kept = keep
        return kept

    @staticmethod
    def _regex(pattern: str) -> re.Pattern[str]:
        parts = re.split(r"(\*\*/|\*\*|\*|\?|\[[^]]*\])", pattern)
        special = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
        return re.compile(
            "".join(
                special.get(part) or (part if part.startswith("[") else re.escape(part))
                for part in parts
            )
        )


@pytest.fixture(scope="module")
def ignore() -> DockerIgnore:
    return DockerIgnore((settings.BASE_DIR / ".dockerignore").read_text(encoding="utf-8"))


def tracked_files(root: Path) -> list[str]:
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        pytest.skip("not a git working copy")
    output = subprocess.run(
        [git, "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return output.splitlines()


def test_every_tracked_file_goes_into_the_image(ignore: DockerIgnore) -> None:
    """Except the folders of LEFT_OUT, which stay out entirely."""
    files = tracked_files(settings.BASE_DIR)

    wrong = [
        path for path in files if ignore.keeps(path) == (PurePosixPath(path).parts[0] in LEFT_OUT)
    ]
    assert wrong == []


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".venv/bin/python",
        "NOTES.md",
        "todo/tasks.json",
        "scripts/local_tool.py",
        "docs/notes.md",
        "media/logos/logo.png",
        "db.sqlite3",
        "static/css/app.css",
        "apps/keys/__pycache__/views.cpython-312.pyc",
        "apps/keys/locale/ru/LC_MESSAGES/django.mo",
    ],
)
def test_a_local_file_stays_out(ignore: DockerIgnore, path: str) -> None:
    assert not ignore.keeps(path)
