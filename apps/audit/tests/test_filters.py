import pytest

from apps.audit.forms import AuditFilterForm
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def events() -> None:
    AuditEvent.objects.create(action="api.call", data={"token": "lmz_ab12"})
    AuditEvent.objects.create(action="api.denied", data={"token": "lmz_cd34"})
    AuditEvent.objects.create(action="key.create")


def actions(**query: str) -> set[str]:
    form = AuditFilterForm(query)
    return set(form.filter(AuditEvent.objects.all()).values_list("action", flat=True))


def test_only_api_events() -> None:
    assert actions(api="only") == {"api.call", "api.denied"}


def test_without_api_events() -> None:
    assert actions(api="hidden") == {"key.create"}


def test_by_the_beginning_of_the_token() -> None:
    assert actions(token=" lmz_ab") == {"api.call"}


def test_without_filters_everything_is_shown() -> None:
    assert actions() == {"api.call", "api.denied", "key.create"}
