from django.urls import path

from apps.guides import views_staff
from apps.guides.views import (
    FaqArticleView,
    FaqListView,
    GuideIndexView,
    MaterialListView,
    PlatformGuideView,
    SuggestView,
)
from apps.guides.views_moderation import (
    SuggestionQueueView,
    SuggestionRejectView,
    SuggestionReviewView,
)

app_name = "guides"

urlpatterns = [
    path("", GuideIndexView.as_view(), name="index"),
    path("faq/", FaqListView.as_view(), name="faq"),
    path("faq/<slug:slug>/", FaqArticleView.as_view(), name="faq-article"),
    path("materials/", MaterialListView.as_view(), name="materials"),
    path("suggest/", SuggestView.as_view(), name="suggest"),
    path("moderation/", SuggestionQueueView.as_view(), name="moderation"),
    path("moderation/<int:pk>/", SuggestionReviewView.as_view(), name="moderation-review"),
    path("moderation/<int:pk>/reject/", SuggestionRejectView.as_view(), name="moderation-reject"),
    path("staff/deleted/", views_staff.DeletedBuiltinView.as_view(), name="staff-deleted"),
    path(
        "staff/deleted/<int:pk>/restore/",
        views_staff.BuiltinRestoreView.as_view(),
        name="staff-restore",
    ),
    path("staff/preview/", views_staff.MarkdownPreviewView.as_view(), name="staff-preview"),
    path("staff/<slug:shelf>/", views_staff.EntryListView.as_view(), name="staff"),
    path("staff/<slug:shelf>/add/", views_staff.EntryFormView.as_view(), name="staff-add"),
    path("staff/<slug:shelf>/<int:pk>/", views_staff.EntryFormView.as_view(), name="staff-edit"),
    path(
        "staff/<slug:shelf>/<int:pk>/publish/",
        views_staff.EntryPublishView.as_view(),
        name="staff-publish",
    ),
    path(
        "staff/<slug:shelf>/<int:pk>/move/",
        views_staff.EntryMoveView.as_view(),
        name="staff-move",
    ),
    path(
        "staff/<slug:shelf>/<int:pk>/delete/",
        views_staff.EntryDeleteView.as_view(),
        name="staff-delete",
    ),
    path("<slug:device>/", PlatformGuideView.as_view(), name="platform"),
]
