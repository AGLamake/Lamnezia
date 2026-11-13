from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User


@admin.register(User)
class AccountAdmin(UserAdmin):  # type: ignore[type-arg]
    """Rights come from the role and a moderator's zone: Django's staff flags, groups and
    permissions are hidden."""

    list_display = ("username", "email", "role", "manager", "is_active", "last_login")
    list_filter = ("role", "is_active")
    filter_horizontal = ("moderated_servers",)
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "email")}),
        (_("Access"), {"fields": ("role", "is_active", "manager")}),
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
