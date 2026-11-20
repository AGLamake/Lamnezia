from django.db import migrations, models
from django.db.models import F


def mark_existing(apps, schema_editor):  # type: ignore[no-untyped-def]
    """Keys issued before the update are not news: they get no `key_connected`."""
    AccessKey = apps.get_model("keys", "AccessKey")  # noqa: N806
    AccessKey.objects.update(connected_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("keys", "0007_accesskey_address"),
    ]

    operations = [
        migrations.AddField(
            model_name="accesskey",
            name="connected_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="first connected at"),
        ),
        migrations.RunPython(mark_existing, migrations.RunPython.noop),
    ]
