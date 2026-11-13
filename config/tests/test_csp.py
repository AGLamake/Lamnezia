"""The CSP forbids inline scripts and eval, so templates must not need them."""

import re
from pathlib import Path

import pytest
from django.conf import settings

TEMPLATES = Path(settings.BASE_DIR) / "templates"
# Inline event handlers (onclick=, onsubmit=...) are blocked by script-src 'self'.
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
ALPINE_ATTR = re.compile(r'\s(?:x-[a-z.:-]+|@[a-z.:-]+|:[a-z-]+)="([^"]*)"')
# The CSP build of Alpine parses expressions itself: no arrow functions, no globals,
# no statement lists. Such code goes to an Alpine.data component in static/js/ui.js.
UNSAFE_EXPRESSION = re.compile(r"=>|\bnavigator\.|\bdocument\.|\bwindow\.|\bNumber\(|;")


def template_files() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_policy_has_no_unsafe_eval_or_inline_scripts() -> None:
    script_src = next(
        part for part in settings.CONTENT_SECURITY_POLICY.split(";") if "script-src" in part
    )

    assert "'unsafe-eval'" not in script_src
    assert "'unsafe-inline'" not in script_src


@pytest.mark.parametrize("path", template_files(), ids=lambda path: path.name)
def test_template_has_no_inline_handlers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert not INLINE_HANDLER.search(text)
    assert "<script>" not in text


@pytest.mark.parametrize("path", template_files(), ids=lambda path: path.name)
def test_alpine_expressions_fit_the_csp_build(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    unsafe = [expr for expr in ALPINE_ATTR.findall(text) if UNSAFE_EXPRESSION.search(expr)]

    assert unsafe == []
