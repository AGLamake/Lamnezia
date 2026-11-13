from django.contrib import admin
from django.http import HttpRequest

from apps.billing.models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Orders are written by payments only; the admin site shows them."""

    list_display = ("created_at", "buyer", "plan", "amount", "currency", "provider", "status")
    list_filter = ("status", "provider")
    search_fields = ("username", "user__username", "number", "external_id")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False
