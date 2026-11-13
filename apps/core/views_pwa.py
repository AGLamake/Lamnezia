"""Install the panel as an app: the web app manifest, the service worker and the offline page."""

import hashlib
from typing import Any, ClassVar

from django.http import HttpRequest, JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import get_language
from django.views import View
from django.views.generic import TemplateView

from apps.core.services.site_settings import cached_settings

# The page colour of the light theme (--ln-canvas in assets/css/app.css): the splash screen and the
# browser bar of the installed app.
LIGHT_CANVAS = "#eef1f6"


class ManifestView(View):
    """The web app manifest: name, icons and how the installed panel opens."""

    ICONS: ClassVar[list[tuple[str, str, str]]] = [
        ("brand/icon-192.png", "192x192", "any"),
        ("brand/icon-512.png", "512x512", "any"),
        ("brand/icon-maskable-512.png", "512x512", "maskable"),
    ]

    def get(self, request: HttpRequest) -> JsonResponse:
        name = cached_settings.get().site_name
        home = reverse("home")
        manifest = {
            "id": home,
            "name": name,
            "short_name": name,
            "lang": get_language(),
            "start_url": home,
            "scope": home,
            "display": "standalone",
            "background_color": LIGHT_CANVAS,
            "theme_color": LIGHT_CANVAS,
            "icons": [
                {"src": static(path), "sizes": sizes, "type": "image/png", "purpose": purpose}
                for path, sizes, purpose in self.ICONS
            ],
        }
        response = JsonResponse(manifest, json_dumps_params={"ensure_ascii": False})
        response["Content-Type"] = "application/manifest+json"
        return response


class ServiceWorkerView(TemplateView):
    """The service worker, served from the root so that its scope is the whole panel.

    It keeps only the offline page and the files it needs: the panel's pages hold keys and
    personal data, so they are never cached.
    """

    template_name = "pwa/sw.js"
    content_type = "text/javascript"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        assets = [
            static(path)
            for path in ("css/app.css", "js/theme.js", "brand/favicon.svg", "brand/icon-192.png")
        ]
        # Static names carry a content hash, so a new release gets a new cache.
        version = hashlib.sha256(" ".join(assets).encode()).hexdigest()[:12]
        return super().get_context_data(
            offline=reverse("pwa-offline"), assets=assets, version=version, **kwargs
        )

    def render_to_response(self, context: dict[str, Any], **kwargs: Any) -> Any:
        response = super().render_to_response(context, **kwargs)
        # The browser checks for a new worker on every visit, not after the static files' cache.
        response["Cache-Control"] = "no-cache"
        return response


class OfflineView(TemplateView):
    """The page the installed app shows when the panel cannot be reached."""

    template_name = "pwa/offline.html"
