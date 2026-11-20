import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.template import Context, Template
from django.test import RequestFactory
from django.views import View

from apps.accounts.models import Role, User
from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import POLICIES, Access, Level, NotAllowedError, Section
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

MODERATOR_DEFAULTS = {
    Section.OVERVIEW: Level.VIEW,
    Section.STATS: Level.VIEW,
    Section.SERVERS: Level.NONE,
    Section.BACKUPS: Level.NONE,
    Section.OPERATIONS: Level.MANAGE,
    Section.USERS: Level.MANAGE,
    Section.KEYS: Level.MANAGE,
    Section.REGISTRATION: Level.MANAGE,
    Section.PLANS: Level.NONE,
    Section.ORDERS: Level.NONE,
    Section.INBOX: Level.MANAGE,
    Section.ARTICLES: Level.NONE,
    Section.AUDIT: Level.NONE,
    Section.STAFF: Level.NONE,
}


def test_policy_covers_every_section() -> None:
    assert set(POLICIES[Role.MODERATOR]) == set(Section.values)


@pytest.mark.parametrize(("section", "level"), MODERATOR_DEFAULTS.items())
def test_empty_access_gives_the_defaults_of_the_role(section: str, level: Level) -> None:
    assert Access(UserFactory(role=Role.MODERATOR)).level(section) == level


@pytest.mark.parametrize("section", Section.values)
def test_administrator_manages_everything(section: str) -> None:
    admin = UserFactory(role=Role.ADMIN, access={section: "none"})
    assert Access(admin).level(section) == Level.MANAGE


@pytest.mark.parametrize("section", Section.values)
def test_portal_user_has_no_section(section: str) -> None:
    assert Access(UserFactory(role=Role.USER, access={section: "manage"})).level(section) == 0


def test_chosen_level_is_cut_to_the_ceiling() -> None:
    moderator = UserFactory(
        role=Role.MODERATOR, access={"servers": "manage", "orders": "view", "keys": "view"}
    )
    access = Access(moderator)
    assert access.level(Section.SERVERS) == Level.VIEW
    assert access.level(Section.ORDERS) == Level.NONE
    assert access.level(Section.KEYS) == Level.VIEW


def test_unknown_values_fall_back_to_the_default() -> None:
    moderator = UserFactory(role=Role.MODERATOR, access={"keys": "everything", "stats": 2})
    access = Access(moderator)
    assert access.level(Section.KEYS) == Level.MANAGE
    assert access.level(Section.STATS) == Level.VIEW
    assert access.level("unknown") == Level.NONE


def test_administrator_grants_up_to_the_ceiling_of_the_role() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    Access(UserFactory(role=Role.ADMIN)).grant(moderator, Section.SERVERS, Level.VIEW)
    moderator.refresh_from_db()
    assert moderator.access == {"servers": "view"}
    assert Access(moderator).level(Section.SERVERS) == Level.VIEW


def test_grant_above_the_ceiling_is_refused() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    with pytest.raises(NotAllowedError):
        Access(UserFactory(role=Role.ADMIN)).grant(moderator, Section.SERVERS, Level.MANAGE)
    moderator.refresh_from_db()
    assert moderator.access == {}


def test_grant_above_own_rights_is_refused() -> None:
    # A member with "staff" to manage still cannot pass on more than they have themselves.
    granter = UserFactory(role=Role.MODERATOR, access={"staff": "manage"})
    target = UserFactory(role=Role.MODERATOR)
    with pytest.raises(NotAllowedError):
        Access(granter).grant(target, Section.SERVERS, Level.VIEW)


def test_moderator_cannot_grant_at_all() -> None:
    granter = UserFactory(role=Role.MODERATOR)
    target = UserFactory(role=Role.MODERATOR, access={"keys": "view"})
    with pytest.raises(NotAllowedError):
        Access(granter).grant(target, Section.KEYS, Level.VIEW)
    target.refresh_from_db()
    assert target.access == {"keys": "view"}


@pytest.mark.parametrize("role", [Role.ADMIN, Role.USER])
def test_only_other_staff_get_rights(role: str) -> None:
    admin = UserFactory(role=Role.ADMIN)
    target = UserFactory(role=role)
    with pytest.raises(NotAllowedError):
        Access(admin).grant(target, Section.KEYS, Level.VIEW)


def test_nobody_grants_themselves() -> None:
    admin = UserFactory(role=Role.ADMIN)
    with pytest.raises(NotAllowedError):
        Access(admin).grant(admin, Section.KEYS, Level.VIEW)


def test_grant_of_an_unknown_section_is_refused() -> None:
    with pytest.raises(NotAllowedError):
        Access(UserFactory(role=Role.ADMIN)).grant(
            UserFactory(role=Role.MODERATOR), "settings", Level.NONE
        )


class ServersPage(SectionRequiredMixin, View):
    section = Section.SERVERS

    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("done")


def outcome(method: str, user: User | AnonymousUser) -> str:
    request = getattr(RequestFactory(), method)("/page/")
    request.user = user
    try:
        response = ServersPage.as_view()(request)
    except PermissionDenied:
        return "denied"
    return "ok" if response.status_code == 200 else "login"


@pytest.mark.parametrize(
    ("level", "get", "post"),
    [("none", "denied", "denied"), ("view", "ok", "denied"), ("manage", "ok", "denied")],
)
def test_mixin_asks_the_level_by_method(level: str, get: str, post: str) -> None:
    # The moderator's ceiling in servers is "view": "manage" is cut down to it.
    moderator = UserFactory(role=Role.MODERATOR, access={"servers": level})
    assert outcome("get", moderator) == get
    assert outcome("post", moderator) == post


def test_mixin_keeps_guests_and_portal_users_out() -> None:
    assert outcome("get", AnonymousUser()) == "login"
    assert outcome("get", UserFactory(role=Role.USER)) == "denied"
    assert outcome("post", UserFactory(role=Role.ADMIN)) == "ok"


def render_can(user: User, section: str, level: str) -> str:
    request = RequestFactory().get("/")
    request.user = user
    template = Template(
        "{% load access %}{% can section level as ok %}{% if ok %}yes{% else %}no{% endif %}"
    )
    return template.render(Context({"request": request, "section": section, "level": level}))


def test_can_tag_gives_the_same_answer_as_access() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    assert render_can(moderator, "keys", "manage") == "yes"
    assert render_can(moderator, "stats", "view") == "yes"
    assert render_can(moderator, "stats", "manage") == "no"
    assert render_can(moderator, "servers", "view") == "no"
