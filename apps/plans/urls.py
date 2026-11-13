from django.urls import path

from apps.plans.views import (
    PlanCreateView,
    PlanDeleteView,
    PlanListView,
    PlanUpdateView,
    UserLimitsView,
    UserLookupView,
)

app_name = "plans"

urlpatterns = [
    path("", PlanListView.as_view(), name="list"),
    path("add/", PlanCreateView.as_view(), name="add"),
    path("<int:pk>/edit/", PlanUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", PlanDeleteView.as_view(), name="delete"),
    path("users/", UserLookupView.as_view(), name="lookup"),
    path("users/<int:pk>/", UserLimitsView.as_view(), name="user"),
]
