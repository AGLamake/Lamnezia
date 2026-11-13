from typing import Any, cast

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.generic import DetailView, FormView, ListView, TemplateView

from apps.accounts.models import User
from apps.guides.forms import SuggestionForm
from apps.guides.models import FaqArticle, UsefulMaterial
from apps.guides.services.devices import DetectedDevice, Device
from apps.guides.services.faq import LocalizedArticle
from apps.guides.services.materials import sections_by_topic
from apps.guides.services.platform_guide import ApkChoice, PlatformGuide
from apps.guides.services.releases import LatestReleaseProvider, StoreLinks
from apps.guides.services.suggestions import (
    MySuggestions,
    SuggestionsClosedError,
    TooManySuggestionsError,
    accepting_suggestions,
)


class DeviceAwareView(TemplateView):
    """Guide page that knows the visitor's device from the User-Agent."""

    def detected(self) -> DetectedDevice:
        return DetectedDevice.from_user_agent(self.request.headers.get("User-Agent", ""))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            devices=list(Device), detected=self.detected().device, **kwargs
        )


class GuideIndexView(DeviceAwareView):
    template_name = "guides/index.html"


class PlatformGuideView(DeviceAwareView):
    """Downloads from the latest Amnezia release; `?android=&abi=` pick the APK."""

    template_name = "guides/platform.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["device"] not in Device.values:
            raise Http404
        device = Device(kwargs.pop("device"))
        apk = ApkChoice(
            self.request.GET.get("android") or self.detected().android_version or "",
            self.request.GET.get("abi", ""),
        )
        guide = PlatformGuide.build(
            device, LatestReleaseProvider.from_settings().latest(), StoreLinks.from_settings(), apk
        )
        return super().get_context_data(guide=guide, **kwargs)


class FaqListView(ListView):  # type: ignore[type-arg]
    template_name = "guides/faq_list.html"
    queryset = FaqArticle.objects.published()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["articles"] = [
            LocalizedArticle(article, get_language()) for article in context["object_list"]
        ]
        return context


class FaqArticleView(DetailView):  # type: ignore[type-arg]
    template_name = "guides/faq_article.html"
    queryset = FaqArticle.objects.published()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            article=LocalizedArticle(self.object, get_language()), **kwargs
        )


class MaterialListView(TemplateView):
    """Outside articles by topic; links the weekly check found broken are not shown."""

    template_name = "guides/materials.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        sections = sections_by_topic(UsefulMaterial.objects.visible(), get_language())
        return super().get_context_data(sections=sections, **kwargs)


class SuggestView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    """A signed-in user suggests a link or a text; their earlier suggestions are listed below."""

    template_name = "guides/suggest.html"
    form_class = SuggestionForm

    @property
    def suggestions(self) -> MySuggestions:
        return MySuggestions(cast(User, self.request.user))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            accepting=accepting_suggestions(),
            mine=self.suggestions.all().select_related("material", "article"),
            **kwargs,
        )

    def form_valid(self, form: SuggestionForm) -> HttpResponse:
        try:
            self.suggestions.send(form.save(commit=False))
        except SuggestionsClosedError:
            messages.error(self.request, _("Suggestions are not accepted now."))
        except TooManySuggestionsError:
            messages.error(self.request, _("Too many suggestions. Try again tomorrow."))
        else:
            messages.success(self.request, _("Thank you! The staff will review your suggestion."))
        return redirect("guides:suggest")
