"""New keys of a group count from their first byte on its members too."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.groups.models import GroupMember
from apps.groups.tests.conftest import Run, succeeded
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start
from apps.traffic.models import TrafficCounter, TrafficSource
from apps.traffic.services.recorder import TrafficRecorder

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(join: Run) -> ProtocolInstance:
    succeeded(join())
    return GroupMember.objects.get(leading=False).instance


def issue(leader: ProtocolInstance, admin: User, start: Start) -> AccessKey:
    operation = start(lambda: KeyActions(admin).issue(leader.source, "phone", None))
    succeeded(operation)
    return AccessKey.objects.get(pk=operation.params["key_id"])


def read_empty(source: str, key_source: str) -> None:
    """A reading of the source with no peers, a minute ago."""
    at = timezone.now() - timedelta(minutes=1)
    TrafficRecorder().record(source, [], at, key_source=key_source)


def test_a_new_key_counts_on_a_member_read_before(
    member: ProtocolInstance, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    read_empty(member.source, leader.source)

    key = issue(leader, admin, start)

    counter = TrafficCounter.objects.get(source=member.source, public_key=key.public_key)
    assert (counter.key_source, counter.rx_bytes, counter.tx_bytes) == (leader.source, 0, 0)


def test_a_member_never_read_keeps_its_first_reading_a_baseline(
    member: ProtocolInstance, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    TrafficRecorder().unavailable(member.source, timezone.now(), leader.source)

    key = issue(leader, admin, start)

    assert TrafficSource.objects.get(source=member.source).available is False
    assert not TrafficCounter.objects.filter(source=member.source, public_key=key.public_key)
