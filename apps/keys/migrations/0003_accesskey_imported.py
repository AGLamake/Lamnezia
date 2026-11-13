from django.db import migrations, models


def mark_imported(apps, schema_editor):
    """Keys without a private key were taken from a server; only Python sees it is empty."""
    AccessKey = apps.get_model("keys", "AccessKey")
    keyless = [key.pk for key in AccessKey.objects.only("private_key") if not key.private_key]
    AccessKey.objects.filter(pk__in=keyless).update(imported=True)


class Migration(migrations.Migration):

    dependencies = [
        ("keys", "0002_drift"),
    ]

    operations = [
        migrations.AddField(
            model_name="accesskey",
            name="imported",
            field=models.BooleanField(default=False, verbose_name="imported"),
        ),
        migrations.RunPython(mark_imported, migrations.RunPython.noop),
    ]
