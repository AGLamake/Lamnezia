"""Test helper for sent letters: the HTML version of a letter."""

from django.core.mail import EmailMessage, EmailMultiAlternatives


def html_version(letter: EmailMessage) -> str:
    """The text/html alternative of `letter`; fails when it has none."""
    assert isinstance(letter, EmailMultiAlternatives)
    [(html, mimetype)] = letter.alternatives
    assert mimetype == "text/html" and isinstance(html, str)
    return html
