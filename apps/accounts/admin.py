from typing import Any

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Management, User


class ManagementInline(admin.TabularInline):  # type: ignore[type-arg]
    """The staff members who serve the user; the quota of each is checked on save."""

    model = Management
    fk_name = "user"
    fields = ("member", "via_plan")
    extra = 0


class AccountChangeForm(UserChangeForm):  # type: ignore[type-arg]
    """A moderator's quotas are no higher than the server administrator's who made them."""

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        for name in ("user_quota", "key_quota"):
            if name in data and (error := self.instance.quota_error(name, data[name])):
                self.add_error(name, error)
        return data


@admin.register(User)
class AccountAdmin(UserAdmin):  # type: ignore[type-arg]
    """Rights come from the role and a moderator's zone: Django's staff flags, groups and
    permissions are hidden."""

    form = AccountChangeForm
    list_display = ("username", "email", "role", "is_active", "last_login")
    list_filter = ("role", "is_active")
    filter_horizontal = ("moderated_servers",)
    inlines = (ManagementInline,)
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "email")}),
        (_("Access"), {"fields": ("role", "is_active")}),
        (
            _("Moderator's zone"),
            {"fields": ("moderated_servers", "user_quota", "key_quota")},
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "role", "usable_password", "password1", "password2"),
            },
        ),
    )
