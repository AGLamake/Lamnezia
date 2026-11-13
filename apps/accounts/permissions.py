"""Access by role for views: a guest goes to the login page, a user with another role gets 403."""

from collections.abc import Callable, Iterable
from functools import wraps
from typing import Any, cast

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseBase

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.audit.services import journal as audit

View = Callable[..., HttpResponseBase]


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
