import re

import pytest
from django.test import Client, override_settings

from apps.dashboard.forms import ShowcaseForm

# Pages read SiteSettings (the panel name in the layouts).
pytestmark = pytest.mark.django_db

DEV_UI = "/dev/ui/"


@override_settings(DEBUG=True)
def test_showcase_renders_admin_layout_with_components(client: Client) -> None:
    html = client.get(DEV_UI).content.decode()

    for marker in (
        "btn btn-primary",
        "badge badge-warning",
        "alert alert-error",
        "table table-cards",
        'hx-get="/dev/ui/modal/"',
        'data-dialog-open="ui-drawer"',
        "data-confirm=",
        'data-toast="success"',
        "empty-state",
        'class="skeleton',
        'class="sparkline"',
        'role="progressbar"',
        'id="confirm-dialog"',
        'class="breadcrumbs"',
    ):
        assert marker in html
    assert "vendor/htmx-2.0.11.min.js" in html
    assert "vendor/alpinejs-csp-3.17.5.min.js" in html
    assert "css/app.css" in html


@override_settings(DEBUG=True)
def test_showcase_switches_to_portal_layout(client: Client) -> None:
    html = client.get(DEV_UI, {"layout": "portal"}).content.decode()

    assert 'class="portal-nav"' in html
    assert "<aside" not in html


@override_settings(DEBUG=True)
def test_modal_fragment_extends_modal_component(client: Client) -> None:
    html = client.get("/dev/ui/modal/").content.decode()

    assert '<dialog class="dialog"' in html
    assert "data-dialog-close" in html
    assert "btn btn-danger" in html
    assert "<html" not in html


@pytest.mark.parametrize("path", [DEV_UI, "/dev/ui/modal/"])
def test_dev_pages_are_hidden_without_debug(client: Client, path: str) -> None:
    assert client.get(path).status_code == 404


def test_form_widgets_get_shared_classes_and_error_state() -> None:
    form = ShowcaseForm(data={"email": "not-an-email"})
    form.is_valid()

    html = str(form)

    assert 'class="input input-invalid"' in html
    assert 'class="checkbox input-invalid"' in html
    assert 'class="error-text"' in html
    assert 'class="label" for="id_name"' in html


def test_field_errors_are_described_by_the_widget() -> None:
    form = ShowcaseForm(data={"email": "not-an-email"})
    form.is_valid()

    html = str(form)

    assert re.search(r'aria-describedby="[^"]*\bid_email_error\b', html)
    assert 'id="id_email_error"' in html


def test_existing_widget_class_is_kept() -> None:
    form = ShowcaseForm()
    form.fields["name"].widget.attrs["class"] = "font-mono"

    assert 'class="input font-mono"' in str(form["name"].as_widget())
