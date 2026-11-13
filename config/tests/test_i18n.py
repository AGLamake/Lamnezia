import pytest
from django.conf import settings
from django.test import Client, override_settings

# Pages read SiteSettings (the panel name in the layouts).
pytestmark = pytest.mark.django_db

DEV_UI = "/dev/ui/"
SET_LANGUAGE = "/i18n/setlang/"


def html_lang(client: Client, **headers: str) -> str:
    """Language of a page with the layouts; so far only the dev UI page has them (needs DEBUG)."""
    html = client.get(DEV_UI, headers=headers).content.decode()
    return html.split('<html lang="', 1)[1].split('"', 1)[0]


@override_settings(DEBUG=True, LANGUAGE_CODE="ru")
def test_russian_is_the_default(client: Client) -> None:
    assert html_lang(client) == "ru"


@override_settings(DEBUG=True)
def test_browser_language_is_used_without_a_choice(client: Client) -> None:
    assert html_lang(client, accept_language="en-US,en;q=0.9") == "en"


@override_settings(DEBUG=True)
@pytest.mark.parametrize("layout", ["admin", "portal"])
def test_switcher_is_in_both_layouts(client: Client, layout: str) -> None:
    html = client.get(DEV_UI, {"layout": layout}).content.decode()

    assert f'action="{SET_LANGUAGE}"' in html
    assert 'name="language" value="en"' in html
    assert 'name="language" value="ru"' in html


@override_settings(DEBUG=True)
@pytest.mark.parametrize("language", ["en", "ru"])
def test_switcher_choice_sticks(client: Client, language: str) -> None:
    other = "ru" if language == "en" else "en"
    response = client.post(SET_LANGUAGE, {"language": language, "next": DEV_UI})

    assert response.status_code == 302
    assert response.headers["Location"] == DEV_UI
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == language
    assert html_lang(client, accept_language=other) == language
