from django.urls import path

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
    path("<slug:device>/", PlatformGuideView.as_view(), name="platform"),
]
