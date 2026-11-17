from django.urls import path

from apps.relocation.views import MoveCancelView, MoveConfirmView, MoveView

app_name = "relocation"

urlpatterns = [
    path("", MoveView.as_view(), name="move"),
    path("<int:pk>/confirm/", MoveConfirmView.as_view(), name="confirm"),
    path("<int:pk>/cancel/", MoveCancelView.as_view(), name="cancel"),
]
