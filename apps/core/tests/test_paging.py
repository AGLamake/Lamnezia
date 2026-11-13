from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpRequest
from django.test import RequestFactory

from apps.core.paging import PageSize


def request(session: SessionStore, **query: str) -> HttpRequest:
    made = RequestFactory().get("/audit/", query)
    made.session = session
    return made


def test_default_is_the_smallest_size() -> None:
    assert PageSize(request(SessionStore())).resolve() == 25


def test_a_chosen_size_is_remembered_for_the_next_lists() -> None:
    session = SessionStore()

    assert PageSize(request(session, per_page="100")).resolve() == 100
    assert PageSize(request(session)).resolve() == 100


def test_a_size_outside_the_choices_is_ignored() -> None:
    session = SessionStore()
    PageSize(request(session, per_page="50")).resolve()

    assert PageSize(request(session, per_page="100000")).resolve() == 50
    assert PageSize(request(session, per_page="lots")).resolve() == 50
