"""The `profile_ready` flag: kept in line with the encrypted profile, so waiting keys are counted
by a query.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.xray_profiles import waiting_profiles

pytestmark = pytest.mark.django_db

SOURCE = "xray:1"
PROFILE = '{"container": "amnezia-xray"}'
BEFORE = [("keys", "0008_accesskey_connected_at")]


def key(name: str, **fields: Any) -> AccessKey:
    fields.setdefault("private_key", f"uuid-{name}")
    fields.setdefault("status", KeyStatus.ACTIVE)
    return AccessKey.objects.create(source=SOURCE, name=name, public_key=name, **fields)


def ready(key: AccessKey) -> bool:
    return AccessKey.objects.values_list("profile_ready", flat=True).get(pk=key.pk)


def test_saving_a_profile_marks_the_key_ready_and_clearing_it_unmarks() -> None:
    waiting = key("phone")
    assert not ready(waiting)

    waiting.profile = PROFILE
    waiting.save(update_fields=["profile"])
    assert ready(waiting)

    waiting.private_key = waiting.profile = ""
    waiting.save(update_fields=["private_key", "profile"])
    assert not ready(waiting)


def test_waiting_keys_are_counted_by_one_query(django_assert_num_queries: Any) -> None:
    key("imported")
    key("disabled", status=KeyStatus.DISABLED_MANUAL)
    key("issued", profile=PROFILE)
    key("keyless", private_key="", imported=True)
    key("revoked", status=KeyStatus.REVOKED)
    AccessKey.objects.create(source="xray:2", name="other", public_key="other", private_key="u")

    with django_assert_num_queries(1):
        assert waiting_profiles(SOURCE) == 2


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate(BEFORE)
    executor.loader.build_graph()
    yield executor
    executor.loader.build_graph()
    executor.migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_the_migration_marks_the_keys_that_have_a_profile(executor: MigrationExecutor) -> None:
    old = executor.loader.project_state(BEFORE).apps.get_model("keys", "AccessKey")
    old.objects.create(source=SOURCE, name="a", public_key="a", private_key="u", profile=PROFILE)
    old.objects.create(source=SOURCE, name="b", public_key="b", private_key="u")

    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())

    flags = dict(AccessKey.objects.values_list("name", "profile_ready"))
    assert flags == {"a": True, "b": False}
