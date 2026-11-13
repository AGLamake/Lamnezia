import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.views import View

from apps.accounts.models import Role
from apps.accounts.permissions import (
    AdminRequiredMixin,
    StaffRequiredMixin,
    admin_required,
    staff_required,
)
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

GUEST = None
ROLES = [GUEST, Role.USER, Role.MODERATOR, Role.ADMIN]


class StaffPage(StaffRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")


class AdminPage(AdminRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")


@staff_required
def staff_page(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


@admin_required
def admin_page(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok")


def request_as(role: Role | None) -> HttpRequest:
    request = RequestFactory().get("/page/")
    request.user = AnonymousUser() if role is None else UserFactory(role=role)
    return request


def outcome(view: object, role: Role | None) -> str:
    """'ok', 'login' (redirect to sign in) or 'denied' (403)."""
    try:
        response = view(request_as(role))  # type: ignore[operator]
    except PermissionDenied:
        return "denied"
    return "ok" if response.status_code == 200 else "login"


@pytest.mark.parametrize(
    ("role", "expected"),
    [(GUEST, "login"), (Role.USER, "denied"), (Role.MODERATOR, "ok"), (Role.ADMIN, "ok")],
)
@pytest.mark.parametrize("view", [StaffPage.as_view(), staff_page])
def test_staff_access(view: object, role: Role | None, expected: str) -> None:
    assert outcome(view, role) == expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [(GUEST, "login"), (Role.USER, "denied"), (Role.MODERATOR, "denied"), (Role.ADMIN, "ok")],
)
@pytest.mark.parametrize("view", [AdminPage.as_view(), admin_page])
def test_admin_access(view: object, role: Role | None, expected: str) -> None:
    assert outcome(view, role) == expected


def test_guest_is_sent_to_login_with_return_address() -> None:
    response = StaffPage.as_view()(request_as(GUEST))

    assert response["Location"] == "/accounts/login/?next=/page/"


@pytest.mark.parametrize(
    ("role", "dashboard", "portal", "django_admin"),
    [
        (GUEST, 302, 302, 302),
        (Role.USER, 403, 200, 302),
        (Role.MODERATOR, 200, 200, 302),
        (Role.ADMIN, 200, 200, 200),
    ],
)
def test_panel_pages_by_role(
    client: Client, role: Role | None, dashboard: int, portal: int, django_admin: int
) -> None:
    if role is not None:
        client.force_login(UserFactory(role=role))

    assert client.get("/dashboard/").status_code == dashboard
    assert client.get("/portal/").status_code == portal
    assert client.get("/admin/").status_code == django_admin


@pytest.mark.parametrize(
    ("role", "target"),
    [(GUEST, "/accounts/login/"), (Role.USER, "/portal/"), (Role.ADMIN, "/dashboard/")],
)
def test_start_page_leads_by_role(client: Client, role: Role | None, target: str) -> None:
    if role is not None:
        client.force_login(UserFactory(role=role))

    assert client.get("/")["Location"] == target
