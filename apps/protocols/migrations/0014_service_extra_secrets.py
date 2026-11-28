"""The additional secrets of MTProxy and Telemt move from the open settings to an encrypted field."""

from django.db import migrations

import apps.ssh.encryption

FIELD = "additional_secrets"


def seal(apps, schema_editor):
    ServiceInstance = apps.get_model("protocols", "ServiceInstance")
    for service in ServiceInstance.objects.filter(settings__has_key=FIELD):
        secrets = service.settings.pop(FIELD)
        if isinstance(secrets, list):
            secrets = ",".join(secrets)
        service.extra_secrets = secrets or ""
        service.save(update_fields=["extra_secrets", "settings"])


def unseal(apps, schema_editor):
    ServiceInstance = apps.get_model("protocols", "ServiceInstance")
    # The field is a Fernet token, so the filter cannot compare it: every row is read.
    for service in ServiceInstance.objects.all():
        if service.extra_secrets:
            service.settings[FIELD] = service.extra_secrets
            service.save(update_fields=["settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("protocols", "0013_sni_scan"),
    ]

    operations = [
        migrations.AddField(
            model_name="serviceinstance",
            name="extra_secrets",
            field=apps.ssh.encryption.EncryptedTextField(
                blank=True, default="", verbose_name="additional secrets"
            ),
        ),
        migrations.RunPython(seal, unseal),
    ]
