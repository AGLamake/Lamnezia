"""FAQ articles in the visitor's language."""

from dataclasses import dataclass

from django.utils.safestring import SafeString

from apps.guides.models import FaqArticle
from apps.guides.services.markdown import render_markdown


@dataclass(frozen=True)
class LocalizedArticle:
    """What templates show of an article; the Markdown is rendered only when asked for."""

    article: FaqArticle
    language: str

    @property
    def slug(self) -> str:
        return self.article.slug

    @property
    def title(self) -> str:
        return self.article.title_in(self.language)

    @property
    def credit(self) -> str:
        return self.article.credit

    @property
    def body_html(self) -> SafeString:
        return render_markdown(self.article.body_in(self.language))
