from django.urls import path

from apps.relocation.views import (
    MoveAbandonView,
    MoveAutoStopView,
    MoveCancelView,
    MoveConfirmView,
    MoveProgressPartView,
    MoveProgressView,
    MoveView,
)

app_name = "relocation"

urlpatterns = [
    path("", MoveView.as_view(), name="move"),
    path("<int:pk>/", MoveProgressView.as_view(), name="progress"),
    path("<int:pk>/progress/", MoveProgressPartView.as_view(), name="progress-part"),
    path("<int:pk>/confirm/", MoveConfirmView.as_view(), name="confirm"),
    path("<int:pk>/cancel/", MoveCancelView.as_view(), name="cancel"),
    path("<int:pk>/abandon/", MoveAbandonView.as_view(), name="abandon"),
    path("<int:pk>/auto-stop/", MoveAutoStopView.as_view(), name="auto-stop"),
]
