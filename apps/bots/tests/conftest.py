from collections.abc import Iterator

import pytest
from django.core.cache import cache

from apps.accounts.models import LinkedAccount, Messenger, User
from apps.bots.services.core import BotCore
from apps.bots.services.session import SiteLinks, Throttle
from apps.bots.tests.fake_adapter import FakeAdapter
from apps.keys.tests.conftest import awg_server  # noqa: F401  (a fixture the key tests share)
from apps.portal.tests.test_portal import subscriber

SITE = "https://vpn.example"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Rate counters and dialog steps live in the cache; no test may see another's."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def bot() -> FakeAdapter:
    return FakeAdapter(core=BotCore(links=SiteLinks(SITE), throttle=Throttle(limit=0)))


def link(user: User, external_id: str = "1001") -> User:
    LinkedAccount.objects.create(user=user, messenger=Messenger.TELEGRAM, external_id=external_id)
    return user


@pytest.fixture
def user() -> User:
    """A linked user with a plan of two keys."""
    return link(subscriber(max_keys=2))
