from django.urls import path

from apps.operations.views import (
    OperationDetailView,
    OperationListView,
    OperationProgressView,
    OperationRetryView,
)

app_name = "operations"

urlpatterns = [
    path("", OperationListView.as_view(), name="list"),
    path("<int:pk>/", OperationDetailView.as_view(), name="detail"),
    path("<int:pk>/progress/", OperationProgressView.as_view(), name="progress"),
    path("<int:pk>/retry/", OperationRetryView.as_view(), name="retry"),
]
