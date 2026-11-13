from django.urls import path

from apps.dashboard import views_links, views_stats, views_users
from apps.dashboard.views import (
    ComponentShowcaseView,
    DashboardView,
    NoticeHideView,
    NoticeShowView,
    QuickSearchView,
    ShowcaseModalView,
)

app_name = "dashboard"

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="index"),
    path("dashboard/search/", QuickSearchView.as_view(), name="search"),
    path("dashboard/notices/hide/", NoticeHideView.as_view(), name="notice-hide"),
    path("dashboard/notices/show/", NoticeShowView.as_view(), name="notices-show"),
    path("stats/", views_stats.StatsView.as_view(), name="stats"),
    path("stats/export.csv", views_stats.StatsExportView.as_view(), name="stats-export"),
    path("users/", views_users.UserListView.as_view(), name="users"),
    path("users/<int:pk>/", views_users.UserDetailView.as_view(), name="user"),
    path(
        "users/<int:pk>/link/<str:messenger>/",
        views_links.UserLinkView.as_view(),
        name="user-link",
    ),
    path(
        "users/<int:pk>/unlink/<str:messenger>/",
        views_links.UserUnlinkView.as_view(),
        name="user-unlink",
    ),
    path(
        "users/<int:pk>/link-code/<str:messenger>/",
        views_links.UserLinkCodeView.as_view(),
        name="user-link-code",
    ),
    path(
        "users/<int:pk>/subscription/",
        views_users.UserSubscriptionView.as_view(),
        name="user-subscription",
    ),
    path("users/batch/", views_users.UserBatchView.as_view(), name="users-batch"),
    path("users/batch/<int:pk>/", views_users.BatchDetailView.as_view(), name="batch"),
    path(
        "users/batch/<int:pk>/progress/",
        views_users.BatchProgressView.as_view(),
        name="batch-progress",
    ),
    path("dev/ui/", ComponentShowcaseView.as_view(), name="dev-ui"),
    path("dev/ui/modal/", ShowcaseModalView.as_view(), name="dev-ui-modal"),
]
