"""Access for views by role and by section: a guest goes to the login page, others get 403."""

from collections.abc import Callable, Iterable
from functools import cached_property, wraps
from typing import Any, cast

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponseBase
from django.views.generic.edit import ProcessFormView

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.accounts.services.access import Access, Level, Section
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit

View = Callable[..., HttpResponseBase]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def check_role(request: HttpRequest, roles: Iterable[str]) -> bool:
    """False for a guest; raises PermissionDenied for a user whose role is not in `roles`."""
    if not isinstance(request.user, User):
        return False
    if request.user.role not in roles:
        raise PermissionDenied
    return True


class RoleRequiredMixin(AccessMixin):
    roles: frozenset[str] = frozenset()
    request: HttpRequest

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not check_role(request, self.roles):
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc, no-any-return]

    @property
    def member(self) -> User:
        """The signed-in user; `dispatch` lets in only users with one of `roles`."""
        return cast(User, self.request.user)

    @property
    def client_ip(self) -> str | None:
        """The visitor address for the audit log of what the view does."""
        return audit.client_ip(self.request)


class StaffRequiredMixin(RoleRequiredMixin):
    roles = STAFF_ROLES


class AdminRequiredMixin(RoleRequiredMixin):
    roles = frozenset({Role.ADMIN})


class SectionRequiredMixin(StaffRequiredMixin):
    """A staff member with `read_level` in `section` opens the view; a request that may change
    something (any method but GET and HEAD) needs `write_level`. A form page is an action itself,
    so it opens with `write_level` too. A view of `whole_zone_only` (objects nobody but the
    administrator answers for, such as server groups) does not exist for anyone else."""

    section: Section
    read_level = Level.VIEW
    write_level = Level.MANAGE
    whole_zone_only = False

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        member = request.user
        if isinstance(member, User) and member.is_staff_member:
            if not Access(member).allows(self.section, self._needed_level(request)):
                raise PermissionDenied
            if self.whole_zone_only and not Zone(member).is_whole:
                raise Http404
        return super().dispatch(request, *args, **kwargs)

    def _needed_level(self, request: HttpRequest) -> Level:
        if request.method in SAFE_METHODS and not isinstance(self, ProcessFormView):
            return self.read_level
        return self.write_level

    @cached_property
    def access(self) -> Access:
        return Access(self.member)

    @cached_property
    def zone(self) -> Zone:
        """Whose objects the member sees; anything outside it answers 404."""
        return Zone(self.member)


def role_required(*roles: str) -> Callable[[View], View]:
    def decorator(view: View) -> View:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            if not check_role(request, roles):
                return redirect_to_login(request.get_full_path())
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


staff_required = role_required(*STAFF_ROLES)
admin_required = role_required(Role.ADMIN)
