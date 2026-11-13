from django.urls import path

from apps.bots import views

app_name = "bots"

urlpatterns = [
    path("", views.status, name="status"),
    path("<str:messenger>/turn/", views.turn, name="turn"),
    path("<str:messenger>/restart/", views.restart, name="restart"),
    path("<str:messenger>/leave/<str:server_id>/", views.leave, name="leave"),
]
