import importlib

import pytest
from django.apps import apps as django_apps

from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.counts import InstanceKeyCount
from apps.keys.tests.conftest import SOURCE

pytestmark = pytest.mark.django_db


def make_key(name: str, status: str = KeyStatus.ACTIVE, *, imported: bool = False) -> AccessKey:
    return AccessKey.objects.create(
        source=SOURCE, name=name, public_key=f"pub-{name}", imported=imported, status=status
    )


def test_count_leaves_out_revoked_and_other_instances() -> None:
    make_key("issued")
    make_key("imported", imported=True)
    make_key("gone", status=KeyStatus.REVOKED, imported=True)
    AccessKey.objects.create(source="awg:2", name="other", public_key="pub-other")

    assert InstanceKeyCount.of(SOURCE) == InstanceKeyCount(total=2, imported=1)


def test_migration_marks_keys_without_private_key_as_imported() -> None:
    migration = importlib.import_module("apps.keys.migrations.0003_accesskey_imported")
    AccessKey.objects.create(source=SOURCE, name="issued", public_key="a", private_key="secret")
    AccessKey.objects.create(source=SOURCE, name="taken", public_key="b")

    migration.mark_imported(django_apps, None)

    assert set(AccessKey.objects.filter(imported=True).values_list("name", flat=True)) == {"taken"}
