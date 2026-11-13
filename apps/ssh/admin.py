from django.contrib import admin

from apps.ssh.models import KnownHost


@admin.register(KnownHost)
class KnownHostAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("host", "port", "key_type", "fingerprint", "created_at")
    search_fields = ("host",)
    readonly_fields = ("host", "port", "key_type", "fingerprint", "created_at")

    def has_add_permission(self, request: object) -> bool:
        # Fingerprints come only from real connections; deleting one re-trusts the server.
        return False
