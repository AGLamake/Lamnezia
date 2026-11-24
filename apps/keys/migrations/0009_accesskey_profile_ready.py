from django.db import migrations, models


def mark_ready(apps, schema_editor):  # type: ignore[no-untyped-def]
    """Keys with a profile get the flag; only Python sees the encrypted profile is filled."""
    AccessKey = apps.get_model("keys", "AccessKey")  # noqa: N806
    ready = [key.pk for key in AccessKey.objects.only("profile") if key.profile]
    AccessKey.objects.filter(pk__in=ready).update(profile_ready=True)


class Migration(migrations.Migration):
    dependencies = [
        ("keys", "0008_accesskey_connected_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="accesskey",
            name="profile_ready",
            field=models.BooleanField(default=False, editable=False, verbose_name="profile ready"),
        ),
        migrations.RunPython(mark_ready, migrations.RunPython.noop),
    ]
