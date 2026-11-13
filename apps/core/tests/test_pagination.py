import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.test import RequestFactory

# The context processors of a request context read the site settings.
pytestmark = pytest.mark.django_db


def render(page: int, **query: str) -> str:
    request = RequestFactory().get("/audit/", {"page": str(page), **query})
    request.user = AnonymousUser()
    page_obj = Paginator(range(30), 10).page(page)
    context = {"is_paginated": True, "page_obj": page_obj}
    return render_to_string("components/pagination.html", context, request=request)


def test_links_keep_the_filters() -> None:
    html = render(2, action="keys.issue", q="a b")

    assert 'href="?page=1&amp;action=keys.issue&amp;q=a+b"' in html
    assert 'href="?page=3&amp;action=keys.issue&amp;q=a+b"' in html


def test_first_page_has_no_previous_link() -> None:
    html = render(1)

    assert 'rel="prev"' not in html
    assert 'rel="next"' in html


def test_rows_per_page_links_start_from_the_first_page() -> None:
    request = RequestFactory().get("/audit/", {"page": "2", "q": "a"})
    request.user = AnonymousUser()
    page_obj = Paginator(range(60), 25).page(2)
    context = {"is_paginated": True, "page_obj": page_obj, "page_sizes": (25, 50, 100)}
    html = render_to_string("components/pagination.html", context, request=request)

    assert 'href="?q=a&amp;per_page=100"' in html
    assert 'href="?q=a&amp;per_page=25" aria-current="page"' in html


def test_nothing_without_pages() -> None:
    assert render_to_string("components/pagination.html", {"is_paginated": False}).strip() == ""
