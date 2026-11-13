from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from apps.accounts.views import home, set_language
from apps.core.views_pwa import ManifestView, OfflineView, ServiceWorkerView
from apps.keys.views_subscription import SubscriptionFeedView
from apps.traffic.views import metrics
from config.health import healthz

urlpatterns = [
    # The admin site signs in through the panel's login, which has the lockout and 2FA.
    path(
        "admin/login/",
        RedirectView.as_view(pattern_name="accounts:login", query_string=True),
    ),
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("manifest.webmanifest", ManifestView.as_view(), name="pwa-manifest"),
    path("sw.js", ServiceWorkerView.as_view(), name="pwa-worker"),
    path("offline/", OfflineView.as_view(), name="pwa-offline"),
    path("metrics", metrics),
    path("accounts/", include("apps.accounts.urls")),
    path("guides/", include("apps.guides.urls")),
    path("audit/", include("apps.audit.urls")),
    path("keys/", include("apps.keys.urls")),
    path("sub/<str:token>", SubscriptionFeedView.as_view(), name="subscription-feed"),
    path("notifications/", include("apps.notifications.urls")),
    path("operations/", include("apps.operations.urls")),
    path("plans/", include("apps.plans.urls")),
    path("servers/", include("apps.servers.urls")),
    path("settings/bots/", include("apps.bots.urls")),
    path("settings/", include("apps.core.urls")),
    path("protocols/", include("apps.protocols.urls")),
    path("backups/", include("apps.backups.urls")),
    path("billing/", include("apps.billing.urls")),
    path("portal/", include("apps.portal.urls")),
    path("support/", include("apps.support.urls")),
    path("i18n/setlang/", set_language, name="set_language"),
    path("", home, name="home"),
    path("", include("apps.dashboard.urls")),
]
