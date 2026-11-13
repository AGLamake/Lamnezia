from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import LinkedAccount, LinkToken, Messenger, NotifyVia
from apps.accounts.services.linking import AccountLinks, LinkError
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

TELEGRAM = Messenger.TELEGRAM


class Clock:
    def __init__(self) -> None:
        self.now = timezone.now()

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def links(clock: Clock) -> AccountLinks:
    return AccountLinks(code_lifetime=timedelta(minutes=10), clock=clock)


def test_a_code_links_the_messenger_account_to_its_user(links: AccountLinks) -> None:
    user = UserFactory()
    code = links.issue_code(user, TELEGRAM)

    account = links.redeem(TELEGRAM, code, "1001", "alice")

    assert (account.user, account.external_id, account.username) == (user, "1001", "alice")


def test_only_the_hash_of_a_code_is_stored(links: AccountLinks) -> None:
    code = links.issue_code(UserFactory(), TELEGRAM)

    assert code not in LinkToken.objects.get().digest


def test_a_code_works_once(links: AccountLinks) -> None:
    code = links.issue_code(UserFactory(), TELEGRAM)
    links.redeem(TELEGRAM, code, "1001")

    with pytest.raises(LinkError):
        links.redeem(TELEGRAM, code, "2002")
    assert LinkedAccount.objects.get().external_id == "1001"


def test_a_code_expires(links: AccountLinks, clock: Clock) -> None:
    code = links.issue_code(UserFactory(), TELEGRAM)
    clock.now += timedelta(minutes=10)

    with pytest.raises(LinkError):
        links.redeem(TELEGRAM, code, "1001")
    assert not LinkedAccount.objects.exists()


def test_a_code_is_for_its_messenger_only(links: AccountLinks) -> None:
    code = links.issue_code(UserFactory(), TELEGRAM)

    with pytest.raises(LinkError):
        links.redeem(Messenger.DISCORD, code, "1001")


def test_a_new_code_replaces_the_previous_one(links: AccountLinks) -> None:
    user = UserFactory()
    first = links.issue_code(user, TELEGRAM)
    second = links.issue_code(user, TELEGRAM)

    with pytest.raises(LinkError):
        links.redeem(TELEGRAM, first, "1001")
    links.redeem(TELEGRAM, second, "1001")


def test_issuing_a_code_removes_expired_ones(links: AccountLinks, clock: Clock) -> None:
    links.issue_code(UserFactory(), TELEGRAM)
    clock.now += timedelta(minutes=11)

    links.issue_code(UserFactory(), TELEGRAM)

    assert LinkToken.objects.count() == 1


def test_an_account_linked_to_another_user_cannot_be_linked_again(links: AccountLinks) -> None:
    owner, other = UserFactory(), UserFactory()
    links.link(owner, TELEGRAM, "1001")
    code = links.issue_code(other, TELEGRAM)

    with pytest.raises(LinkError):
        links.redeem(TELEGRAM, code, "1001")
    assert LinkedAccount.objects.get().user == owner


def test_linking_again_replaces_the_users_account(links: AccountLinks) -> None:
    user = UserFactory()
    links.link(user, TELEGRAM, "1001", "old")

    links.link(user, TELEGRAM, "2002", "new")

    assert LinkedAccount.objects.get().external_id == "2002"


def test_a_user_can_link_both_messengers(links: AccountLinks) -> None:
    user = UserFactory()
    links.link(user, TELEGRAM, "1001")
    links.link(user, Messenger.DISCORD, "1001")

    assert user.linked_accounts.count() == 2


def test_unlinking_sends_notifications_to_email_again(links: AccountLinks) -> None:
    user = UserFactory(notify_via=NotifyVia.TELEGRAM)
    links.link(user, TELEGRAM, "1001")

    assert links.unlink(user, TELEGRAM)

    assert not LinkedAccount.objects.exists()
    assert user.notify_via == NotifyVia.EMAIL


def test_the_user_of_an_account_must_be_active(links: AccountLinks) -> None:
    user = UserFactory()
    links.link(user, TELEGRAM, "1001")
    assert AccountLinks.user_of(TELEGRAM, "1001") == user

    user.is_active = False
    user.save()

    assert AccountLinks.user_of(TELEGRAM, "1001") is None
