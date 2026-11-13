from django.urls import path

from apps.keys.views import (
    DriftCheckView,
    DriftListView,
    DriftResolveView,
    KeyActionView,
    KeyAssignView,
    KeyBulkView,
    KeyDetailView,
    KeyFileView,
    KeyIssueView,
    KeyListView,
    KeyResetFirstViewView,
    KeyRevealView,
)

app_name = "keys"

urlpatterns = [
    path("", KeyListView.as_view(), name="list"),
    path("new/", KeyIssueView.as_view(), name="issue"),
    path("bulk/", KeyBulkView.as_view(), name="bulk"),
    path("drift/", DriftListView.as_view(), name="drift"),
    path("drift/check/", DriftCheckView.as_view(), name="drift-check"),
    path("drift/<int:pk>/<str:action>/", DriftResolveView.as_view(), name="drift-resolve"),
    path("<int:pk>/", KeyDetailView.as_view(), name="detail"),
    path("<int:pk>/show/", KeyRevealView.as_view(), name="reveal"),
    path("<int:pk>/file/<slug:extension>/", KeyFileView.as_view(), name="file"),
    path("<int:pk>/assign/", KeyAssignView.as_view(), name="assign"),
    path("<int:pk>/reset-view/", KeyResetFirstViewView.as_view(), name="reset-view"),
    path("<int:pk>/<str:action>/", KeyActionView.as_view(), name="action"),
]
