"""User suggestions for the guides: sending, the limits, the staff review and the notifications."""

from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.audit.models import AuditEvent
from apps.guides.forms import (
    ArticlePublishForm,
    MaterialPublishForm,
    PublishAs,
    SuggestionForm,
    publish_form,
)
from apps.guides.models import (
    FaqArticle,
    Suggestion,
    SuggestionKind,
    SuggestionStatus,
    UsefulMaterial,
)
from apps.guides.services.faq import LocalizedArticle
from apps.guides.services.suggestions import (
    MySuggestions,
    SuggestionReview,
    SuggestionsClosedError,
    TooManySuggestionsError,
)
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, Notification, Route

pytestmark = pytest.mark.django_db

LINK: dict[str, Any] = {
    "kind": SuggestionKind.LINK,
    "title": "Speeding up AmneziaWG",
    "url": "https://www.example.com/awg-speed",
    "language": "en",
    "topic": "speed",
    "comment": "Helped me on LTE",
}
TEXT: dict[str, Any] = {
    "kind": SuggestionKind.TEXT,
    "title": "Ключ на телевизоре",
    "body": "Установите **Amnezia** из магазина.",
    "language": "ru",
    "topic": "devices",
}


def suggestion(*, author: User | None = None, **fields: Any) -> Suggestion:
    data = LINK | fields
    return Suggestion.objects.create(author=author or UserFactory(), **data)


def valid_form(**data: Any) -> SuggestionForm:
    form = SuggestionForm(data)
    assert form.is_valid(), form.errors
    return form


class TestSuggestionForm:
    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "ftp://example.com/a"],
    )
    def test_only_http_links_are_taken(self, url: str) -> None:
        form = SuggestionForm(LINK | {"url": url})

        assert "url" in form.errors

    def test_a_link_suggestion_needs_the_link(self) -> None:
        assert "url" in SuggestionForm(LINK | {"url": ""}).errors

    def test_a_text_suggestion_needs_the_text(self) -> None:
        assert "body" in SuggestionForm(TEXT | {"body": "  "}).errors

    def test_the_published_material_takes_only_http_links_too(self) -> None:
        form = MaterialPublishForm({"url": "javascript:alert(1)"})

        assert "url" in form.errors


class TestSending:
    def test_the_staff_channels_hear_of_a_new_suggestion(self) -> None:
        channel = Channel.objects.create(name="Ops", kind=ChannelKind.WEBHOOK, target="https://x")
        Route.objects.create(channel=channel, event=Event.SUGGESTION_CREATED)
        user = UserFactory()

        MySuggestions(user).send(valid_form(**LINK).save(commit=False))

        notification = Notification.objects.get(event=Event.SUGGESTION_CREATED)
        assert notification.data == {
            "user": user.username,
            "kind": "Link to an article",
            "title": "Speeding up AmneziaWG",
        }

    @override_settings(SUGGESTION_RATE_LIMIT=2)
    def test_a_user_sends_at_most_the_limit_a_day(self) -> None:
        user = UserFactory()
        suggestions = MySuggestions(user)
        suggestions.send(valid_form(**LINK).save(commit=False))
        suggestions.send(valid_form(**TEXT).save(commit=False))

        with pytest.raises(TooManySuggestionsError):
            suggestions.send(valid_form(**LINK).save(commit=False))
        assert MySuggestions(UserFactory()).send(valid_form(**LINK).save(commit=False)).pk

    def test_nothing_is_taken_when_suggestions_are_off(self) -> None:
        configure(guide_suggestions=False)

        with pytest.raises(SuggestionsClosedError):
            MySuggestions(UserFactory()).send(valid_form(**LINK).save(commit=False))
        assert not Suggestion.objects.exists()


class TestPage:
    def test_a_guest_is_sent_to_sign_in(self, client: Client) -> None:
        response = client.post(reverse("guides:suggest"), LINK)

        assert response.status_code == 302
        assert response["Location"].startswith("/accounts/login/")
        assert not Suggestion.objects.exists()

    def test_a_user_sends_a_suggestion_and_sees_it_in_the_list(self, client: Client) -> None:
        user = UserFactory()
        client.force_login(user)

        response = client.post(reverse("guides:suggest"), LINK, follow=True)

        assert Suggestion.objects.get().author == user
        assert "Speeding up AmneziaWG" in response.content.decode()

    def test_the_page_says_when_suggestions_are_off(self, client: Client) -> None:
        configure(guide_suggestions=False)
        client.force_login(UserFactory())

        response = client.get(reverse("guides:suggest"))
        client.post(reverse("guides:suggest"), LINK)

        assert "Suggestions are not accepted now." in response.content.decode()
        assert 'name="title"' not in response.content.decode()
        assert not Suggestion.objects.exists()

    @override_settings(SUGGESTION_RATE_LIMIT=1)
    def test_over_the_limit_the_user_is_told_to_wait(self, client: Client) -> None:
        user = UserFactory()
        suggestion(author=user)
        client.force_login(user)

        response = client.post(reverse("guides:suggest"), TEXT, follow=True)

        assert Suggestion.objects.count() == 1
        assert "Try again tomorrow" in response.content.decode()


class TestPublishForm:
    def test_a_link_becomes_a_material_filled_from_the_suggestion(self) -> None:
        form = publish_form(suggestion(credit_author=True))

        assert isinstance(form, MaterialPublishForm)
        assert form.initial["url"] == LINK["url"]
        assert form.initial["source"] == "example.com"
        assert form.initial["slug"] == "speeding-up-amneziawg"
        assert form.initial["credit"]

    def test_a_text_becomes_an_article_in_its_language(self) -> None:
        form = publish_form(suggestion(**TEXT))

        assert isinstance(form, ArticlePublishForm)
        assert form.initial["title_ru"] == TEXT["title"]
        assert form.initial["body_ru"] == TEXT["body"]
        assert form.initial["slug"].startswith("suggestion-")

    def test_the_author_is_not_named_unless_they_asked(self) -> None:
        assert publish_form(suggestion(credit_author=False)).initial["credit"] == ""

    def test_a_link_can_become_an_article_that_points_to_it(self) -> None:
        form = publish_form(suggestion(), target=PublishAs.ARTICLE)

        assert isinstance(form, ArticlePublishForm)
        assert form.initial["title_en"] == LINK["title"]
        assert form.initial["body_en"] == f"[{LINK['title']}]({LINK['url']})"

    def test_a_text_can_become_a_material_described_by_it(self) -> None:
        form = publish_form(suggestion(**TEXT), target=PublishAs.MATERIAL)

        assert isinstance(form, MaterialPublishForm)
        assert form.initial["description_ru"] == TEXT["body"]
        assert form.initial["topic"] == TEXT["topic"]

    def test_an_unknown_choice_falls_back_to_what_was_sent(self) -> None:
        assert isinstance(publish_form(suggestion(), target="poem"), MaterialPublishForm)


def material_data(**fields: Any) -> dict[str, Any]:
    return {
        "slug": "awg-speed",
        "title": "Speeding up AmneziaWG",
        "url": LINK["url"],
        "language": "en",
        "source": "example.com",
        "topic": "speed",
        "description_ru": "Как ускорить AmneziaWG.",
        "credit": "",
    } | fields


class TestReview:
    def test_publishing_a_link_makes_a_material_and_tells_the_author(self) -> None:
        reviewer = UserFactory(role=Role.MODERATOR)
        sent = suggestion()
        form = publish_form(sent, material_data(credit="Anna"))
        assert form.is_valid(), form.errors

        SuggestionReview(reviewer, ip="203.0.113.5").publish(sent, form.save(commit=False))

        sent.refresh_from_db()
        material = UsefulMaterial.objects.get(slug="awg-speed")
        assert (sent.status, sent.material, sent.reviewed_by) == (
            SuggestionStatus.ACCEPTED,
            material,
            reviewer,
        )
        assert material.credit == "Anna"
        told = Notification.objects.get(event=Event.SUGGESTION_ACCEPTED)
        assert told.user == sent.author
        assert told.data["url"].endswith("/guides/materials/")
        event = AuditEvent.objects.get(action="guides.suggestion_accept")
        assert (event.actor, event.ip, event.target_id) == (reviewer, "203.0.113.5", str(sent.pk))

    def test_rejecting_keeps_the_reason_and_tells_the_author(self) -> None:
        reviewer = UserFactory(role=Role.ADMIN)
        sent = suggestion()

        SuggestionReview(reviewer).reject(sent, "Already in the list")

        sent.refresh_from_db()
        assert (sent.status, sent.reject_reason) == (
            SuggestionStatus.REJECTED,
            "Already in the list",
        )
        told = Notification.objects.get(event=Event.SUGGESTION_REJECTED)
        assert (told.user, told.data["reason"]) == (sent.author, "Already in the list")
        assert AuditEvent.objects.get(action="guides.suggestion_reject").data == {
            "reason": "Already in the list",
            "author": sent.author.username,
        }


class TestModerationPages:
    @pytest.fixture
    def moderator(self, client: Client) -> User:
        user = UserFactory(role=Role.MODERATOR)
        client.force_login(user)
        return user

    def test_the_queue_shows_waiting_suggestions(self, client: Client, moderator: User) -> None:
        sent = suggestion()

        response = client.get(reverse("guides:moderation"))

        assert reverse("guides:moderation-review", args=[sent.pk]) in response.content.decode()

    def test_dangerous_markdown_is_cut_from_the_preview(
        self, client: Client, moderator: User
    ) -> None:
        body = "Hi <script>alert(1)</script> [x](javascript:alert(1)) <img src=x onerror=alert(1)>"
        sent = suggestion(**TEXT | {"body": body})

        page = client.get(reverse("guides:moderation-review", args=[sent.pk])).content.decode()
        preview = page.split('<div class="markdown">', 1)[1].split("</div>", 1)[0]

        assert "<script" not in preview
        assert "javascript:" not in preview
        assert "onerror" not in preview

    def test_the_moderator_edits_and_publishes_a_text_as_an_faq_article(
        self, client: Client, moderator: User
    ) -> None:
        sent = suggestion(**TEXT)
        data = {"slug": "tv-key", "title_ru": TEXT["title"], "body_ru": "Исправленный текст."}

        response = client.post(reverse("guides:moderation-review", args=[sent.pk]), data)

        assert response.status_code == 302
        article = FaqArticle.objects.get(slug="tv-key")
        assert article.body_ru == "Исправленный текст."
        assert client.get(reverse("guides:faq-article", args=["tv-key"])).status_code == 200

    def test_the_moderator_publishes_a_link_as_an_faq_article(
        self, client: Client, moderator: User
    ) -> None:
        sent = suggestion()
        page = reverse("guides:moderation-review", args=[sent.pk])
        assert 'name="body_en"' in client.get(page, {"as": "article"}).content.decode()

        data = {"as": "article", "slug": "awg-speed", "title_en": "Speed", "body_en": "Read it."}
        response = client.post(page, data)

        assert response.status_code == 302
        sent.refresh_from_db()
        assert sent.article == FaqArticle.objects.get(slug="awg-speed")
        assert not UsefulMaterial.objects.exists()

    def test_a_published_article_strips_dangerous_markdown(self) -> None:
        article = FaqArticle(slug="x", title_ru="X", body_ru="<script>alert(1)</script>ok")

        assert "<script" not in LocalizedArticle(article, "ru").body_html

    def test_an_invalid_form_is_shown_again(self, client: Client, moderator: User) -> None:
        sent = suggestion()

        response = client.post(
            reverse("guides:moderation-review", args=[sent.pk]), material_data(url="ftp://x.org")
        )

        assert response.status_code == 200
        assert not UsefulMaterial.objects.exists()

    def test_the_moderator_declines_with_a_reason(self, client: Client, moderator: User) -> None:
        sent = suggestion()

        client.post(reverse("guides:moderation-reject", args=[sent.pk]), {"reason": "Off topic"})

        sent.refresh_from_db()
        assert (sent.status, sent.reject_reason) == (SuggestionStatus.REJECTED, "Off topic")

    def test_declining_needs_a_reason(self, client: Client, moderator: User) -> None:
        sent = suggestion()

        client.post(reverse("guides:moderation-reject", args=[sent.pk]), {"reason": ""})

        sent.refresh_from_db()
        assert sent.status == SuggestionStatus.PENDING

    def test_a_decided_suggestion_is_not_decided_again(
        self, client: Client, moderator: User
    ) -> None:
        sent = suggestion(status=SuggestionStatus.REJECTED)

        response = client.post(reverse("guides:moderation-review", args=[sent.pk]), material_data())

        assert response.status_code == 404
        assert not UsefulMaterial.objects.exists()

    def test_the_author_sees_the_published_credit(self, client: Client) -> None:
        UsefulMaterial.objects.create(**material_data(credit="Anna"))

        page = client.get(reverse("guides:materials")).content.decode()

        assert "Anna" in page
